"""Span-producing and context-propagating wrappers for oslo.messaging.

The instrumentation has two thin halves:

* **Producer — span + context injection.** :func:`inject_wrapper` wraps
  ``Transport._send`` / ``Transport._send_notification``, opening a ``PRODUCER``
  span named after the operation (the RPC method or the notification event
  type). By that point the per-message context has *already* been serialized
  into the plain dictionary that goes on the wire, so injecting W3C trace
  context there is reliable and serializer-agnostic (it works with any
  ``Serializer`` subclass, including production's ``RequestContextSerializer``
  whose ``to_dict()`` would otherwise drop unknown keys).

* **Consumer — span + context extraction.** :func:`rpc_server_wrapper` and
  :func:`notification_server_wrapper` wrap the dispatchers, opening a
  ``CONSUMER`` span parented to the producer via the context carried on
  ``incoming.ctxt``. The span is scoped to the dispatch call, so no context is
  attached beyond message handling.

Attribute names and values track oslo.messaging's own built-in tracing
(``oslo_messaging/_tracing``) so spans from either emitter are queryable
together: ``messaging.operation`` carries the call style, the destination is the
target topic, the RPC namespace lives in ``rpc.service`` rather than being
folded into ``rpc.method``, and the request id is recorded as
``openstack.request_id``.

``messaging.system`` names the *broker* the configured driver talks to
(``rabbitmq``, ``kafka``, ...), not the client library -- that is what the
semantic conventions ask for, and it is what makes a trace comparable with
spans from non-OpenStack producers on the same broker. It is resolved per span:
from the transport's own parsed URL on the producer side, and from the
driver-specific class of the received message on the consumer side, which never
sees a transport. ``rpc.system`` stays ``oslo.messaging`` -- that genuinely is
the RPC system, whichever broker carries it.

Every wrapper is a ``wrapt``-style function ``(wrapped, instance, args, kwargs)``
and never raises on its own account: telemetry must not break the host
application, so attribute extraction is guarded against the narrow expected
failure modes and the wrapped callable is always invoked. Exceptions raised by
the wrapped callable propagate unchanged and are recorded on the active span.
"""

from typing import Any, Callable, Mapping, Optional

from opentelemetry import context, propagate, trace
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.semconv._incubating.attributes import (
    messaging_attributes,
    rpc_attributes,
)
from opentelemetry.semconv._incubating.attributes.messaging_attributes import (
    MessagingSystemValues,
)
from opentelemetry.semconv.trace import MessagingOperationValues
from opentelemetry.trace import SpanKind, Tracer
from opentelemetry.trace.span import Span

__all__ = [
    "inject_trace",
    "rpc_server_wrapper",
    "notification_server_wrapper",
]

#: ``rpc.system`` value: the RPC system *is* oslo.messaging, whichever broker
#: it is configured to talk to.
RPC_SYSTEM = "oslo.messaging"

#: ``messaging.system`` value used when the configured driver cannot be
#: determined. The broker is the right answer here (see
#: :func:`_messaging_system_for_driver`), so this is only a last resort.
DEFAULT_MESSAGING_SYSTEM = "oslo.messaging"

#: oslo.messaging driver names whose semantic-convention ``messaging.system``
#: value differs from the driver name itself. Anything else is reported
#: verbatim: ``kafka`` is already the conventional value, and ``amqp`` (AMQP
#: 1.0) and ``fake`` have no conventional value but name themselves better than
#: any fallback would.
_MESSAGING_SYSTEM_BY_DRIVER = {
    "rabbit": MessagingSystemValues.RABBITMQ.value,
    "kombu": MessagingSystemValues.RABBITMQ.value,  # entry-point alias
}

#: Driver module of an ``IncomingMessage``, mapped to the driver name. The
#: consumer side never sees a ``Transport``, so the class of the message it is
#: handed is what identifies the broker it arrived from.
_DRIVER_BY_INCOMING_MODULE = {
    "oslo_messaging._drivers.amqpdriver": "rabbit",
    "oslo_messaging._drivers.impl_kafka": "kafka",
    "oslo_messaging._drivers.impl_fake": "fake",
}

# oslo.messaging-specific span attributes (no semantic-convention equivalent).
ATTR_NOTIFICATION_PRIORITY = "oslo_messaging.notification.priority"
ATTR_NOTIFICATION_EVENT_TYPE = "oslo_messaging.notification.event_type"
ATTR_NOTIFICATION_PUBLISHER_ID = "oslo_messaging.notification.publisher_id"
ATTR_TARGET_EXCHANGE = "oslo_messaging.target.exchange"

#: The OpenStack request id. oslo.messaging's own tracing records the request id
#: under this name, so spans from either emitter can be correlated the same way.
ATTR_OPENSTACK_REQUEST_ID = "openstack.request_id"

# ``messaging.operation`` values, matching those oslo.messaging's built-in
# tracing records: the RPC call style for a send, ``receive`` for a dispatch.
# Notifications are neither a call nor a cast, so they are plain sends.
_OP_CALL = "call"
_OP_CAST = "cast"
_OP_SEND = "send"
_OP_RECEIVE = "receive"

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


def _messaging_system_for_driver(driver: Optional[str]) -> str:
    """Map an oslo.messaging driver name to a ``messaging.system`` value.

    :param driver: The driver name from the transport URL (``rabbit``,
        ``kafka``, ...), or ``None`` when it could not be determined. A
        compound scheme (``rabbit+ssl``) is reduced to its driver the same way
        oslo.messaging itself resolves one.
    :returns: The ``messaging.system`` value to record.
    """
    if not driver:
        return DEFAULT_MESSAGING_SYSTEM
    driver = driver.split("+")[0]
    return _MESSAGING_SYSTEM_BY_DRIVER.get(driver, driver)


def _producer_messaging_system(transport: Any) -> str:
    """The ``messaging.system`` of the transport a message is being sent on.

    Read from the transport's own parsed URL, so it reflects the driver this
    transport was actually configured with -- a service whose notifications go
    to a different broker than its RPC gets the right value on each.

    :param transport: The ``Transport`` instance handling the send.
    :returns: The ``messaging.system`` value to record.
    """
    driver = getattr(transport, "_driver", None)
    url = getattr(driver, "_url", None)
    return _messaging_system_for_driver(getattr(url, "transport", None))


def _consumer_messaging_system(incoming: Any) -> str:
    """The ``messaging.system`` a received message arrived from.

    Dispatchers are handed a driver-specific ``IncomingMessage`` and never see
    the transport, so the message's own class is what identifies the broker.

    :param incoming: The ``IncomingMessage`` being dispatched.
    :returns: The ``messaging.system`` value to record.
    """
    # Walk the MRO, not just the concrete class: a driver may subclass another
    # driver's message type (and out-of-tree drivers subclass the in-tree ones).
    for klass in type(incoming).__mro__:
        driver = _DRIVER_BY_INCOMING_MODULE.get(klass.__module__)
        if driver is not None:
            return _messaging_system_for_driver(driver)
    return DEFAULT_MESSAGING_SYSTEM


def _rpc_method(message: Mapping) -> Optional[str]:
    """The dotted RPC method name from an outgoing RPC message, if present.

    An RPC message dict carries the remote ``method`` (and optional
    ``namespace``); a notification message does not, so this returns ``None``
    for notifications.

    :param message: The message dict about to be sent.
    :returns: ``namespace.method`` (or just ``method``), or ``None``.
    """
    if not isinstance(message, Mapping):
        return None
    rpc_method = message.get("method")
    if not rpc_method:
        return None
    namespace = message.get("namespace")
    return f"{namespace}.{rpc_method}" if namespace else str(rpc_method)


def inject_trace(tracer: Tracer, is_notification: bool = False):

    def inject_wrapper(
        wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        """Open a ``PRODUCER`` span and inject trace context into the message.

        Wraps ``Transport._send`` (RPC) or ``Transport._send_notification``
        (notifications). The span is named after the operation being sent -- the
        RPC method (``start_instance send``) or the notification event type
        (``compute.instance.create.start send``), falling back to ``send`` when
        neither is available (never ``None send``). The already-serialized
        context dict (second positional argument) gets a ``traceparent`` entry
        so the consumer can continue the trace.

        :param wrapped: The original transport send method.
        :param instance: The bound ``Transport`` instance.
        :param args: Positional arguments ``(target, ctxt, message, ...)``.
        :param kwargs: Keyword arguments to the send method.

        :returns: Whatever the wrapped send method returns.
        """
        if not is_instrumentation_enabled():
            return wrapped(*args, **kwargs)

        # Keep the original ``ctxt`` object: injection must mutate the very dict
        # that goes on the wire, so it cannot be replaced with ``or {}``.
        ctxt = _arg(args, 1, kwargs, "ctxt")
        target = _arg(args, 0, kwargs, "target")
        message = _arg(args, 2, kwargs, "message")
        if not isinstance(message, Mapping):
            message = {}

        if is_notification:
            event_type = message.get("event_type")
            dest = f"{event_type} send" if event_type else "send"
        else:
            rpc_method = _rpc_method(message)
            dest = f"{rpc_method} send" if rpc_method else "send"

        span = tracer.start_span(name=dest, kind=SpanKind.PRODUCER)
        if span.is_recording():
            system = _producer_messaging_system(instance)
            # The topic is where the message is actually routed, and is what
            # oslo.messaging's own tracing reports as the destination. The
            # exchange is kept under an oslo-specific name -- it scopes the
            # topic, and no semantic convention covers it.
            if topic := getattr(target, "topic", None):
                span.set_attribute(
                    messaging_attributes.MESSAGING_DESTINATION_NAME, topic
                )
            if exchange := getattr(target, "exchange", None):
                span.set_attribute(ATTR_TARGET_EXCHANGE, exchange)
            if is_notification:
                _set_notification_attributes(
                    span,
                    system=system,
                    operation=_OP_SEND,
                    ctxt=ctxt,
                    event_type=message.get("event_type"),
                    priority=message.get("priority"),
                    publisher_id=message.get("publisher_id"),
                )
            else:
                # ``wait_for_reply`` is what distinguishes a call from a cast.
                _set_rpc_attributes(
                    span,
                    ctxt=ctxt,
                    method=message.get("method"),
                    namespace=message.get("namespace"),
                    system=system,
                    operation=(
                        _OP_CALL
                        if _arg(args, 3, kwargs, "wait_for_reply")
                        else _OP_CAST
                    ),
                )

        with trace.use_span(span, end_on_exit=True):
            # Only a mapping can carry the injected ``traceparent`` on the wire.
            if isinstance(ctxt, Mapping):
                propagate.inject(ctxt)
            result = wrapped(*args, **kwargs)
        return result

    return inject_wrapper


def _set_rpc_attributes(
    span: Span,
    *,
    ctxt: dict,
    method: Optional[str],
    namespace: Optional[str],
    system: str,
    operation: str,
) -> None:
    """Record RPC span attributes (shared by the producer and consumer sides).

    ``rpc.method`` is the bare method and the namespace goes to ``rpc.service``,
    which is both what the semantic conventions describe and what
    oslo.messaging's own tracing records. The dotted ``namespace.method`` form
    is still what names the span, where it disambiguates same-named methods.

    :param span: The recording span to enrich.
    :param ctxt: The message context, carrying the OpenStack request id.
    :param method: The remote method being sent/handled, if known.
    :param namespace: The RPC namespace the method belongs to, if any.
    :param system: The ``messaging.system`` value for the broker in use.
    :param operation: The ``messaging.operation`` value for this span.
    """
    span.set_attribute(messaging_attributes.MESSAGING_SYSTEM, system)
    span.set_attribute(messaging_attributes.MESSAGING_OPERATION, operation)
    span.set_attribute(
        messaging_attributes.MESSAGING_OPERATION_NAME, operation
    )
    span.set_attribute(rpc_attributes.RPC_SYSTEM, RPC_SYSTEM)
    if method is not None:
        span.set_attribute(rpc_attributes.RPC_METHOD, str(method))
    if namespace:
        span.set_attribute(rpc_attributes.RPC_SERVICE, str(namespace))
    _set_request_id(span, ctxt)


def _set_request_id(span: Span, ctxt: Any) -> None:
    """Record the OpenStack request id, if the context carries one.

    Recorded twice on purpose: ``openstack.request_id`` is what oslo.messaging's
    own tracing emits, and ``messaging.message.conversation_id`` is the portable
    semantic-convention attribute for the same correlation id.

    :param span: The recording span to enrich.
    :param ctxt: The message context; anything non-mapping is ignored.
    :returns: ``None``.
    """
    if not isinstance(ctxt, Mapping):
        return
    request_id = ctxt.get("request_id")
    if not request_id:
        return
    span.set_attribute(ATTR_OPENSTACK_REQUEST_ID, str(request_id))
    span.set_attribute(
        messaging_attributes.MESSAGING_MESSAGE_CONVERSATION_ID,
        str(request_id),
    )


def _set_notification_attributes(
    span: Span,
    *,
    system: str,
    operation: str,
    ctxt: Any = None,
    event_type: Optional[str],
    priority: Optional[str],
    publisher_id: Optional[str],
) -> None:
    """Record notification span attributes (never the payload).

    :param span: The recording span to enrich.
    :param system: The ``messaging.system`` value for the broker in use.
    :param operation: The ``messaging.operation`` value for this span.
    :param ctxt: The message context, carrying the OpenStack request id.
    :param event_type: The notification event type, if known.
    :param priority: The notification priority, if known.
    :param publisher_id: The publisher identifier, if known.
    """
    span.set_attribute(messaging_attributes.MESSAGING_SYSTEM, system)
    span.set_attribute(messaging_attributes.MESSAGING_OPERATION, operation)
    span.set_attribute(
        messaging_attributes.MESSAGING_OPERATION_NAME, operation
    )
    _set_request_id(span, ctxt)
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

        rpc_method = message.get("method", "")
        namespace = message.get("namespace", "")
        qualified = f"{namespace}.{rpc_method}" if namespace else rpc_method
        dest = f"{qualified} {MessagingOperationValues.RECEIVE.value}"

        # Detach in ``finally``: an RPC handler raising is routine (client
        # errors, ``ExpectedException``, timeouts), and executors hand messages
        # to a *pool* of threads or greenthreads. Leaving the message context
        # attached would pin that worker to a finished message's trace, and the
        # next message without a ``traceparent`` would inherit it.
        token = context.attach(span_ctx)
        try:
            span = tracer.start_span(name=dest, kind=SpanKind.CONSUMER)
            _set_rpc_attributes(
                span,
                ctxt=ctxt,
                method=rpc_method,
                namespace=namespace,
                system=_consumer_messaging_system(incoming),
                operation=_OP_RECEIVE,
            )
            if message_id := message.get("msg_id"):
                span.set_attribute(
                    messaging_attributes.MESSAGING_MESSAGE_ID, message_id
                )

            with trace.use_span(span, end_on_exit=True):
                return wrapped(*args, **kwargs)
        finally:
            context.detach(token)

    return wrapper


def notification_server_wrapper(tracer: Tracer) -> Wrapper:
    """Build a wrapper for ``NotificationDispatcher.dispatch`` (consumer side).

    Produces a ``"<event_type> receive"`` (``CONSUMER``) span parented to the
    producer's span via the context carried on ``incoming.ctxt``. The span is
    scoped to the dispatch call. The payload is never recorded.

    .. note::

        Only the single-message dispatcher is wrapped; the batch dispatcher
        (:class:`oslo_messaging.notify.dispatcher.BatchNotificationDispatcher`)
        overrides ``dispatch`` with a list-valued signature and is left
        un-instrumented.

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
        if not isinstance(message, Mapping):
            message = {}

        span_ctx = propagate.extract(ctxt)
        if not span_ctx:
            span_ctx = context.get_current()

        event_type = message.get("event_type", "")
        dest = f"{event_type} {MessagingOperationValues.RECEIVE.value}"

        # See ``rpc_server_wrapper``: the attach has to be undone even when the
        # handler raises, or a pooled worker keeps the finished message's trace.
        token = context.attach(span_ctx)
        try:
            span = tracer.start_span(name=dest, kind=SpanKind.CONSUMER)
            _set_notification_attributes(
                span,
                system=_consumer_messaging_system(incoming),
                operation=_OP_RECEIVE,
                ctxt=ctxt,
                event_type=message.get("event_type"),
                priority=message.get("priority"),
                publisher_id=message.get("publisher_id"),
            )
            if message_id := message.get("message_id"):
                span.set_attribute(
                    messaging_attributes.MESSAGING_MESSAGE_ID, message_id
                )

            with trace.use_span(span, end_on_exit=True):
                return wrapped(*args, **kwargs)
        finally:
            context.detach(token)

    return wrapper
