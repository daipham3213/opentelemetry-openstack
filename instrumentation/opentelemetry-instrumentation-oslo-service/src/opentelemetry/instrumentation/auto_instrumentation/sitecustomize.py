import os

from opentelemetry.instrumentation.auto_instrumentation import initialize

#: Opt-in flag: when ``"true"`` (case-insensitive), eventlet is monkey-patched.
OTEL_PYTHON_EVENTLET_MONKEY_PATCH = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

#: Backend forced for oslo.service once eventlet has been patched.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"


flag = os.environ.get(OTEL_PYTHON_EVENTLET_MONKEY_PATCH) or "false"
if flag.strip().lower() == "true":
    try:
        eventlet = __import__("eventlet")
        eventlet.monkey_patch()
        # Forced (not ``setdefault``): a stale threading value would
        # re-introduce the fork/socket mismatch the patch prevents.
        os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
    except ImportError:
        pass


initialize()
