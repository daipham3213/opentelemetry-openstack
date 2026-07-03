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

# eventlet greenthread spawns to patch, as ``(owner, method, callable_index)``.
# OpenStack services (e.g. nova-compute's ``build_and_run_instance``) hand the
# real work to a greenthread right after an RPC is dispatched; eventlet gives
# each greenthread its own contextvars, so without this the work would start on
# a fresh, disconnected trace. ``callable_index`` is where the spawned function
# sits in the positional args (``spawn_after`` takes a delay first).
_EVENTLET_SPAWNS: Tuple[Tuple[Any, str, int], ...] = ()

try:
    import eventlet
    from eventlet.greenpool import GreenPool

    _EVENTLET_SPAWNS = (
        (eventlet, "spawn", 0),
        (eventlet, "spawn_n", 0),
        (eventlet, "spawn_after", 1),
        (GreenPool, "spawn", 0),
        (GreenPool, "spawn_n", 0),
    )
except ImportError:
    eventlet = None
    GreenPool = None
    _EVENTLET_SPAWNS = ()


class OsloMessagingInstrumentor(BaseInstrumentor):
    """Instrument oslo.messaging RPC and notification transports."""

    def instrumentation_dependencies(self) -> Collection[str]:
        """Return the distributions this instrumentor depends on."""
        return _INSTRUMENTS

    def _instrument(self, **kwargs: Any) -> None:
        """Patch oslo.messaging to inject context and emit consumer spans.

        Also propagates the active trace context across eventlet greenthreads so
        work an RPC handler spawns stays on the request's trace.

        :keyword tracer_provider: Optional
            :class:`opentelemetry.trace.TracerProvider` overriding the global
            provider.
        """
        tracer_provider = kwargs.get("tracer_provider")
        tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider=tracer_provider,
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

        if _WRAPPED_METHODS:
            # Producer: open a PRODUCER span and inject trace context into the
            # on-the-wire context dict.
            wrapt.wrap_function_wrapper(
                Transport,
                "_send",
                decorators.inject_trace(tracer),
            )
            wrapt.wrap_function_wrapper(
                Transport,
                "_send_notification",
                decorators.inject_trace(tracer, is_notification=True),
            )

            # Consumer: open spans parented to the producer.
            wrapt.wrap_function_wrapper(
                RPCDispatcher,
                "dispatch",
                decorators.rpc_server_wrapper(tracer),
            )
            wrapt.wrap_function_wrapper(
                NotificationDispatcher,
                "dispatch",
                decorators.notification_server_wrapper(tracer),
            )

        # Carry the active trace context across eventlet greenthreads so work a
        # handler hands off (e.g. nova's build_and_run_instance) continues the
        # same trace instead of starting a disconnected one.
        for owner, name, func_index in _EVENTLET_SPAWNS:
            wrapt.wrap_function_wrapper(
                owner, name, decorators.spawn_wrapper(func_index)
            )

    def _uninstrument(self, **kwargs: Any) -> None:
        """Restore all patched oslo.messaging and eventlet entry points."""
        for owner, name in _WRAPPED_METHODS:
            unwrap(owner, name)
        for owner, name, _ in _EVENTLET_SPAWNS:
            unwrap(owner, name)
