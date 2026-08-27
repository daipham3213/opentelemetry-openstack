try:
    import _oslo_service_eventlet
except ImportError:
    # No eventlet, no patch. This is a no-op, not an error.
    _oslo_service_eventlet = None

if _oslo_service_eventlet is not None:
    # Patch before importing auto_instrumentation -- that import pulls in the
    # socket stack, which the eventlet patch must precede. This also installs
    # the fork hook and hands the SDK real threads, both gated on the same
    # opt-in: without it we leave the process entirely alone.
    _oslo_service_eventlet.try_patch()

import os  # noqa: E402

# Select this distro explicitly. ``_load_distro`` takes the *first*
# ``opentelemetry_distro`` entry point when ``OTEL_PYTHON_DISTRO`` is unset, and
# upstream's "distro" sorts ahead of "oslo_service" -- so without this the
# OpenStack distro (and, through it, the configurator it pins and the OTLP
# protocol default it sets) never runs at all.
os.environ.setdefault("OTEL_PYTHON_DISTRO", "oslo_service")

from opentelemetry.instrumentation import auto_instrumentation  # noqa: E402

auto_instrumentation.initialize()
