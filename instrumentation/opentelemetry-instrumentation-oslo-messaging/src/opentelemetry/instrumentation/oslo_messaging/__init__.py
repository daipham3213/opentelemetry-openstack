"""OpenTelemetry instrumentation for oslo.messaging.

Usage::

    from opentelemetry.instrumentation.oslo_messaging import (
        OsloMessagingInstrumentor,
    )

    OsloMessagingInstrumentor().instrument()

The instrumentor patches two thin layers:

**Producer — context injection.** ``Transport._send`` and
``Transport._send_notification`` inject the active W3C trace context into the
already-serialized context dictionary that goes on the wire. This is
serializer-agnostic (it runs *after* the serializer) and covers every send
path: RPC calls, casts, fanout, replies, and notifications. No producer span is
created — the linkage rides on whatever span is active in the caller.

**Consumer — spans.** ``RPCDispatcher.dispatch`` and
``NotificationDispatcher.dispatch`` open ``CONSUMER`` spans parented to the
producer, extracting context from the incoming message:

* ``RPCDispatcher.dispatch`` → ``oslo.messaging.rpc.process``
* ``NotificationDispatcher.dispatch`` → ``oslo.messaging.notification.process``

Message payloads are never recorded as span attributes.
"""

from logging import getLogger
from typing import Any, Collection, Tuple

import wrapt

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.oslo_messaging import decorators
from opentelemetry.instrumentation.oslo_messaging.version import __version__
from opentelemetry.instrumentation.utils import unwrap

__all__ = ["OsloMessagingInstrumentor", "__version__"]

_LOG = getLogger(__name__)
_INSTRUMENTS = ("oslo.messaging",)

# (class, method_name) pairs patched via ``wrapt`` and restored with ``unwrap``.
_WRAPPED_METHODS: Tuple[Tuple[type, str], ...] = ()

try:
    from oslo_messaging.notify.dispatcher import NotificationDispatcher
    from oslo_messaging.rpc.dispatcher import RPCDispatcher
    from oslo_messaging.transport import Transport

    _WRAPPED_METHODS = (
        (Transport, "_send"),
        (Transport, "_send_notification"),
        (RPCDispatcher, "dispatch"),
        (NotificationDispatcher, "dispatch"),
    )
except ImportError:
    NotificationDispatcher = None
    RPCDispatcher = None
    Transport = None

    _WRAPPED_METHODS = ()
    _LOG.warning(
        "oslo.messaging is not installed; oslo.messaging instrumentation "
        "will be disabled"
    )


class OsloMessagingInstrumentor(BaseInstrumentor):
    """Instrument oslo.messaging RPC and notification transports."""

    def instrumentation_dependencies(self) -> Collection[str]:
        """Return the distributions this instrumentor depends on."""
        return _INSTRUMENTS

    def _instrument(self, **kwargs: Any) -> None:
        """Patch oslo.messaging to inject context and emit consumer spans.

        :keyword tracer_provider: Optional
            :class:`opentelemetry.trace.TracerProvider` overriding the global
            provider.
        """
        if not _WRAPPED_METHODS:
            # oslo.messaging is not available; nothing to instrument.
            return

        tracer_provider = kwargs.get("tracer_provider")
        tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider=tracer_provider,
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

        # Producer: inject trace context into the on-the-wire context dict.
        wrapt.wrap_function_wrapper(
            Transport,
            "_send",
            decorators.inject_trace(tracer),
        )
        wrapt.wrap_function_wrapper(
            Transport,
            "_send_notification",
            decorators.inject_trace(tracer),
        )

        # Consumer: open spans parented to the producer.
        wrapt.wrap_function_wrapper(
            RPCDispatcher, "dispatch", decorators.rpc_server_wrapper(tracer)
        )
        wrapt.wrap_function_wrapper(
            NotificationDispatcher,
            "dispatch",
            decorators.notification_server_wrapper(tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        """Restore all patched oslo.messaging entry points."""
        for owner, name in _WRAPPED_METHODS:
            unwrap(owner, name)
