try:
    import _oslo_service_eventlet
except ImportError:
    # No eventlet, no patch. This is a no-op, not an error.
    _oslo_service_eventlet = None

if _oslo_service_eventlet is not None:
    # Patch before importing auto_instrumentation -- that import pulls in the
    # socket stack, which the eventlet patch must precede.
    _oslo_service_eventlet.try_patch()

import os  # noqa: E402

# Select this distro explicitly. ``_load_distro`` takes the *first*
# ``opentelemetry_distro`` entry point when ``OTEL_PYTHON_DISTRO`` is unset, and
# upstream's "distro" sorts ahead of "oslo_service" -- so without this the
# OpenStack distro (and, through it, the configurator it pins and the OTLP
# protocol default it sets) never runs at all.
os.environ.setdefault("OTEL_PYTHON_DISTRO", "oslo_service")

from opentelemetry.instrumentation import auto_instrumentation  # noqa: E402

if _oslo_service_eventlet is not None:
    # Fold the patch into auto_instrumentation._initialize itself, so a later
    # call to initialize() -- re-entrant, or made directly by application
    # code -- re-applies it too.
    _oslo_service_eventlet.wrap_initialize(auto_instrumentation)

auto_instrumentation.initialize()
