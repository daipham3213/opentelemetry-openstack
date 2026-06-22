"""OpenTelemetry instrumentation for oslo.messaging.

oslo.messaging is OpenStack's RPC and notification library. This instrumentor
patches the client-side entry points so outgoing messages are traced and the
active trace context travels with the message to the remote side:

* :meth:`RPCClient.call` produces a ``CLIENT`` span
  ``oslo.messaging.rpc.call``;
* :meth:`RPCClient.cast` produces a ``PRODUCER`` span
  ``oslo.messaging.rpc.cast``;
* :meth:`Notifier._notify` produces a ``PRODUCER`` span
  ``oslo.messaging.notification.publish``.

For every traced call the current context is injected into the request
context's ``_otel_context`` carrier, so a consumer instrumented the same way can
extract it and continue the trace. Message payloads are never recorded as span
attributes.

Usage::

    from opentelemetry.instrumentation.oslo_messaging import (
        OsloMessagingInstrumentor,
    )

    OsloMessagingInstrumentor().instrument()
"""

from collections.abc import MutableMapping
from functools import wraps
from typing import Collection

from opentelemetry import propagate, trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.oslo_messaging.version import __version__
from opentelemetry.trace import SpanKind

try:
    from oslo_messaging.notify.notifier import Notifier
    from oslo_messaging.rpc.client import RPCClient
except ImportError:
    Notifier = None
    RPCClient = None

_instruments = ("oslo.messaging",)


def _target_attributes(target):
    """Extract span attributes from an oslo.messaging ``Target``."""
    attributes = {}
    if target is None:
        return attributes

    for name in ("exchange", "topic", "server", "namespace", "version"):
        value = getattr(target, name, None)
        if value is not None:
            attributes[f"oslo_messaging.{name}"] = value

    return attributes


def _rpc_attributes(client, operation, method):
    """Build span attributes for an RPC ``call``/``cast``."""
    attributes = {
        "messaging.system": "oslo.messaging",
        "messaging.operation.name": operation,
        "rpc.system": "oslo.messaging",
    }

    if method is not None:
        attributes["rpc.method"] = method

    attributes.update(_target_attributes(getattr(client, "target", None)))
    return attributes


def _notification_attributes(notifier, priority, event_type):
    """Build span attributes for a notification publish."""
    attributes = {
        "messaging.system": "oslo.messaging",
        "messaging.operation.name": "publish",
    }

    if priority is not None:
        attributes["oslo_messaging.notification.priority"] = priority
    if event_type is not None:
        attributes["oslo_messaging.notification.event_type"] = event_type

    publisher_id = getattr(notifier, "publisher_id", None)
    if publisher_id is not None:
        attributes["oslo_messaging.notification.publisher_id"] = publisher_id

    return attributes


def _inject_context(context):
    """Inject the current trace context into the message's request context.

    The carrier is stored under ``_otel_context`` so the remote side can pull it
    back out and continue the trace. No-op when ``context`` is not a mapping.
    """
    if not isinstance(context, MutableMapping):
        return

    carrier = context.setdefault("_otel_context", {})
    if isinstance(carrier, MutableMapping):
        propagate.inject(carrier)


def _wrap_rpc_method(method, operation, span_kind, tracer):
    """Wrap ``RPCClient.call``/``cast`` to trace and propagate context."""

    @wraps(method)
    def traced_rpc_method(self, ctxt, method_name, *args, **kwargs):
        with tracer.start_as_current_span(
            f"oslo.messaging.rpc.{operation}",
            kind=span_kind,
            attributes=_rpc_attributes(self, operation, method_name),
            record_exception=True,
            set_status_on_exception=True,
        ):
            _inject_context(ctxt)
            return method(self, ctxt, method_name, *args, **kwargs)

    return traced_rpc_method


def _wrap_notify(method, tracer):
    """Wrap ``Notifier._notify`` to trace and propagate context."""

    @wraps(method)
    def traced_notify(
        self, ctxt, event_type, payload, priority, *args, **kwargs
    ):
        with tracer.start_as_current_span(
            "oslo.messaging.notification.publish",
            kind=SpanKind.PRODUCER,
            attributes=_notification_attributes(self, priority, event_type),
            record_exception=True,
            set_status_on_exception=True,
        ):
            _inject_context(ctxt)
            return method(
                self, ctxt, event_type, payload, priority, *args, **kwargs
            )

    return traced_notify


class OsloMessagingInstrumentor(BaseInstrumentor):
    """An instrumentor for oslo.messaging RPC and notification clients.

    Args:
        tracer_provider: ``TracerProvider`` used to obtain the tracer. Defaults
            to the global provider.
    """

    _original_rpc_call = None
    _original_rpc_cast = None
    _original_notify = None

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        if RPCClient is None or Notifier is None:
            return  # oslo.messaging is not available

        if OsloMessagingInstrumentor._original_rpc_call is not None:
            return  # already instrumented

        tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider=kwargs.get("tracer_provider"),
        )

        OsloMessagingInstrumentor._original_rpc_call = RPCClient.call
        OsloMessagingInstrumentor._original_rpc_cast = RPCClient.cast
        OsloMessagingInstrumentor._original_notify = Notifier._notify

        RPCClient.call = _wrap_rpc_method(
            OsloMessagingInstrumentor._original_rpc_call,
            "call",
            SpanKind.CLIENT,
            tracer,
        )
        RPCClient.cast = _wrap_rpc_method(
            OsloMessagingInstrumentor._original_rpc_cast,
            "cast",
            SpanKind.PRODUCER,
            tracer,
        )
        Notifier._notify = _wrap_notify(
            OsloMessagingInstrumentor._original_notify,
            tracer,
        )

    def _uninstrument(self, **kwargs):
        if OsloMessagingInstrumentor._original_rpc_call is None:
            return

        RPCClient.call = OsloMessagingInstrumentor._original_rpc_call
        RPCClient.cast = OsloMessagingInstrumentor._original_rpc_cast
        Notifier._notify = OsloMessagingInstrumentor._original_notify

        OsloMessagingInstrumentor._original_rpc_call = None
        OsloMessagingInstrumentor._original_rpc_cast = None
        OsloMessagingInstrumentor._original_notify = None


__all__ = ["OsloMessagingInstrumentor", "__version__"]
