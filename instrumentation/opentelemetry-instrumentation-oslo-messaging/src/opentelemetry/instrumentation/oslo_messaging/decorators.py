"""Span-producing and context-propagating wrappers for oslo.messaging.

The instrumentation has two thin halves:

* **Producer — context injection.** :func:`inject_wrapper` wraps
  ``Transport._send`` / ``Transport._send_notification``. By that point the
  per-message context has *already* been serialized into the plain dictionary
  that goes on the wire, so injecting W3C trace context there is reliable and
  serializer-agnostic (it works with any ``Serializer`` subclass, including
  production's ``RequestContextSerializer`` whose ``to_dict()`` would otherwise
  drop unknown keys). No span is produced — the linkage rides on whatever span
  is already active in the caller.

* **Consumer — span + context extraction.** :func:`rpc_server_wrapper` and
  :func:`notification_server_wrapper` wrap the dispatchers, opening a
  ``CONSUMER`` span parented to the producer via the context carried on
  ``incoming.ctxt``. The span is scoped to the dispatch call, so no context is
  attached beyond message handling.

Every wrapper is a ``wrapt``-style function ``(wrapped, instance, args, kwargs)``
and never raises on its own account: telemetry must not break the host
application, so attribute extraction is guarded against the narrow expected
failure modes and the wrapped callable is always invoked. Exceptions raised by
the wrapped callable propagate unchanged and are recorded on the active span.
"""

import logging
from typing import Any, Callable, Mapping, Optional

from opentelemetry import context, propagate, trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.semconv._incubating.attributes import (
    messaging_attributes,
    rpc_attributes,
)
from opentelemetry.semconv.trace import MessagingOperationValues
from opentelemetry.trace import SpanKind, Tracer
from opentelemetry.trace.span import Span

__all__ = [
    "inject_trace",
    "rpc_server_wrapper",
    "notification_server_wrapper",
]

_LOG = logging.getLogger(__name__)

#: ``messaging.system`` / ``rpc.system`` value identifying this transport.
MESSAGING_SYSTEM = "oslo.messaging"

# oslo.messaging-specific span attributes (no semantic-convention equivalent).
ATTR_NOTIFICATION_PRIORITY = "oslo_messaging.notification.priority"
ATTR_NOTIFICATION_EVENT_TYPE = "oslo_messaging.notification.event_type"
ATTR_NOTIFICATION_PUBLISHER_ID = "oslo_messaging.notification.publisher_id"

# Operation name recorded under ``messaging.operation.name`` for consumer spans.
_OP_PROCESS = "process"

# Narrow failure modes for attribute/dict access. Catching these (rather than a
# broad ``Exception``) keeps real bugs visible while ensuring a malformed
# message can never break the host application's messaging path.
_EXTRACTION_ERRORS = (AttributeError, KeyError, TypeError, IndexError)

WrappedFn = Callable[..., Any]
Wrapper = Callable[[WrappedFn, Any, tuple, dict], Any]


def _arg(args: tuple, index: int, kwargs: dict, name: str) -> Any:
    """Return a positional-or-keyword argument, preferring the positional one.

    :param args: The positional arguments passed to the wrapped callable.
    :param index: The positional index the argument would occupy.
    :param kwargs: The keyword arguments passed to the wrapped callable.
    :param name: The keyword name of the argument.

    :returns: The argument value, or ``None`` if it was supplied neither way.
    """
    if len(args) > index:
        return args[index]
    return kwargs.get(name)


def inject_trace(tracer: Tracer):

    def inject_wrapper(
        wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        """Inject the active trace context into an outgoing message's context.

        Wraps ``Transport._send`` / ``Transport._send_notification``. The second
        positional argument is the already-serialized context dictionary about to
        go on the wire; a ``traceparent`` entry is added to it when a span is
        active. No span is created here.

        :param wrapped: The original transport send method.
        :param instance: The bound ``Transport`` instance.
        :param args: Positional arguments ``(target, ctxt, message, ...)``.
        :param kwargs: Keyword arguments to the send method.

        :returns: Whatever the wrapped send method returns.
        """
        if not is_instrumentation_enabled():
            return wrapped(*args, **kwargs)

        # The linkage rides on whatever span is already active in the caller;
        # with no active span there is nothing to propagate, so leave the
        # outgoing message untouched.
        if not trace.get_current_span().get_span_context().is_valid:
            return wrapped(*args, **kwargs)

        ctxt = _arg(args, 1, kwargs, "ctxt") or {}
        method = _arg(args, 2, kwargs, "method") or {}
        target = _arg(args, 0, kwargs, "target")

        rpc_method = method.get("method")
        namespace = method.get("namespace")
        if namespace:
            rpc_method = f"{namespace}.{rpc_method}"

        dest = f"{rpc_method} send"
        span = tracer.start_span(name=dest, kind=SpanKind.PRODUCER)

        span.set_attribute(
            messaging_attributes.MESSAGING_DESTINATION_TEMPLATE, True
        )
        span.set_attribute(
            messaging_attributes.MESSAGING_DESTINATION_NAME,
            getattr(target, "exchange", None),
        )
        _set_rpc_attributes(span, ctxt=ctxt, method=rpc_method)
        with trace.use_span(span, end_on_exit=True):
            # Only a mapping can carry the injected ``traceparent`` on the wire.
            if isinstance(ctxt, Mapping):
                propagate.inject(ctxt)
            result = wrapped(*args, **kwargs)
        return result

    return inject_wrapper


def _remote_context(carrier: Any) -> Optional[Context]:
    """Extract the producer's trace context from an incoming wire context.

    :param carrier: The ``ctxt`` mapping carried with an incoming message.

    :returns: The extracted context to use as the consumer span's parent, or
        ``None`` if the carrier is not a mapping (no context to extract).
    """
    if isinstance(carrier, Mapping):
        return propagate.extract(carrier)
    return None


def _set_rpc_attributes(span: Span, ctxt: dict, method: Optional[str]) -> None:
    """Record RPC consumer span attributes.

    :param span: The recording span to enrich.
    :param method: The remote method being handled, if known.
    """
    span.set_attribute(messaging_attributes.MESSAGING_SYSTEM, MESSAGING_SYSTEM)
    span.set_attribute(
        messaging_attributes.MESSAGING_OPERATION_NAME, _OP_PROCESS
    )
    span.set_attribute(rpc_attributes.RPC_SYSTEM, MESSAGING_SYSTEM)
    if method is not None:
        span.set_attribute(rpc_attributes.RPC_METHOD, str(method))
    if isinstance(ctxt, Mapping) and (request_id := ctxt.get("request_id")):
        span.set_attribute(
            messaging_attributes.MESSAGING_MESSAGE_CONVERSATION_ID, request_id
        )


def _set_notification_attributes(
    span: Span,
    *,
    event_type: Optional[str],
    priority: Optional[str],
    publisher_id: Optional[str],
) -> None:
    """Record notification consumer span attributes (never the payload).

    :param span: The recording span to enrich.
    :param event_type: The notification event type, if known.
    :param priority: The notification priority, if known.
    :param publisher_id: The publisher identifier, if known.
    """
    span.set_attribute(messaging_attributes.MESSAGING_SYSTEM, MESSAGING_SYSTEM)
    span.set_attribute(
        messaging_attributes.MESSAGING_OPERATION_NAME, _OP_PROCESS
    )
    if priority is not None:
        span.set_attribute(ATTR_NOTIFICATION_PRIORITY, priority)
    if event_type is not None:
        span.set_attribute(ATTR_NOTIFICATION_EVENT_TYPE, event_type)
    if publisher_id is not None:
        span.set_attribute(ATTR_NOTIFICATION_PUBLISHER_ID, publisher_id)


def rpc_server_wrapper(tracer: Tracer) -> Wrapper:
    """Build a wrapper for ``RPCDispatcher.dispatch`` (consumer side).

    Produces an ``oslo.messaging.rpc.process`` (``CONSUMER``) span parented to
    the producer's span. The span is scoped to the dispatch call.

    :param tracer: The tracer used to create spans.
    :returns: A ``wrapt``-style wrapper
        ``(wrapped, instance, args, kwargs) -> Any``.
    """

    def wrapper(
        wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if not is_instrumentation_enabled():
            return wrapped(*args, **kwargs)

        incoming = _arg(args, 0, kwargs, "incoming")
        ctxt = getattr(incoming, "ctxt", None) or {}
        message = getattr(incoming, "message", None) or {}

        span_ctx = propagate.extract(ctxt)
        if not span_ctx:
            span_ctx = context.get_current()

        token = context.attach(span_ctx)

        rpc_method = message.get("method", "")
        namespace = message.get("namespace", "")
        if namespace:
            rpc_method = f"{namespace}.{rpc_method}"
        dest = f"{rpc_method} {MessagingOperationValues.RECEIVE.value}"

        span = tracer.start_span(name=dest, kind=SpanKind.CONSUMER)
        _set_rpc_attributes(span, ctxt=ctxt, method=rpc_method)
        if message_id := message.get("msg_id"):
            span.set_attribute(
                messaging_attributes.MESSAGING_MESSAGE_ID, message_id
            )

        span.set_attribute(
            messaging_attributes.MESSAGING_OPERATION,
            MessagingOperationValues.RECEIVE.value,
        )

        with trace.use_span(span, end_on_exit=True):
            result = wrapped(*args, **kwargs)

        if token:
            context.detach(token)
        return result

    return wrapper


def notification_server_wrapper(tracer: Tracer) -> Wrapper:
    """Build a wrapper for ``NotificationDispatcher.dispatch`` (consumer side).

    Produces an ``oslo.messaging.notification.process`` (``CONSUMER``) span
    parented to the producer's span. The payload is never recorded.

    .. note::

        Only the single-message dispatcher is wrapped; the batch dispatcher
        (:class:`oslo_messaging.notify.dispatcher.BatchNotificationDispatcher`)
        overrides ``dispatch`` with a list-valued signature and is left
        un-instrumented.

    :param tracer: The tracer used to create spans.
    :returns: A ``wrapt``-style wrapper
        ``(wrapped, instance, args, kwargs) -> Any``.
    """
    span_name = f"{MESSAGING_SYSTEM}.notification.{_OP_PROCESS}"

    def wrapper(
        wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if not is_instrumentation_enabled():
            return wrapped(*args, **kwargs)

        incoming = _arg(args, 0, kwargs, "incoming")
        ctxt = getattr(incoming, "ctxt", None)
        message = getattr(incoming, "message", None) or {}
        if not isinstance(message, Mapping):
            message = {}

        with tracer.start_as_current_span(
            span_name, context=_remote_context(ctxt), kind=SpanKind.CONSUMER
        ) as span:
            if span.is_recording():
                try:
                    _set_notification_attributes(
                        span,
                        event_type=message.get("event_type"),
                        priority=message.get("priority"),
                        publisher_id=message.get("publisher_id"),
                    )
                except _EXTRACTION_ERRORS:  # pragma: no cover - defensive
                    _LOG.debug(
                        "failed to set notification server span attributes",
                        exc_info=True,
                    )
            return wrapped(*args, **kwargs)

    return wrapper
