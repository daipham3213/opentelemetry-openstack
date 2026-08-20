try:
    import _oslo_service_eventlet

    # Patch before importing ``initialize`` -- that import pulls in the socket
    # stack, which the eventlet patch must precede.
    _oslo_service_eventlet.try_patch()
except ImportError:
    # No eventlet, no patch. This is a no-op, not an error.
    pass

from opentelemetry.instrumentation.auto_instrumentation import (  # noqa: E402
    initialize,
)

initialize()
