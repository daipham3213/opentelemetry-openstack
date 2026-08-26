"""Eventlet monkey-patch, factored out so it can run at the earliest hook."""

import os
import warnings

__all__ = [
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
    "try_patch",
    "wrap_initialize",
]

#: Opt-in flag: when ``"true"`` (case-insensitive), eventlet is monkey-patched.
OTEL_PYTHON_EVENTLET_MONKEY_PATCH = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

#: Backend forced for oslo.service once eventlet has been patched.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"


def try_patch() -> None:
    """Monkey-patch eventlet when opted in, and pin the oslo.service backend.

    No-op unless ``OTEL_PYTHON_EVENTLET_MONKEY_PATCH`` is ``"true"``
    (case-insensitive). A missing ``eventlet`` is surfaced with
    :func:`warnings.warn` rather than the module logger on purpose: this runs
    during startup, before the logging pipeline is configured, so a warning is
    the reliably visible channel for the misconfiguration.

    :returns: ``None``.
    """
    flag = os.environ.get(OTEL_PYTHON_EVENTLET_MONKEY_PATCH) or "false"
    if flag.strip().lower() != "true":
        return

    try:
        eventlet = __import__("eventlet")
        eventlet.monkey_patch()
        # Forced (not ``setdefault``): a stale threading value would
        # re-introduce the fork/socket mismatch the patch prevents.
        os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
    except ImportError:
        warnings.warn(
            f"{OTEL_PYTHON_EVENTLET_MONKEY_PATCH} is set to true, but "
            "eventlet is not installed; continuing without "
            "monkey-patching.",
            stacklevel=2,
        )


def wrap_initialize(auto_instrumentation_module) -> None:
    """Fold :func:`try_patch` into ``auto_instrumentation_module._initialize``.

    :param auto_instrumentation_module: the already-imported
        ``opentelemetry.instrumentation.auto_instrumentation`` module.
    :returns: ``None``.
    """
    original = auto_instrumentation_module._initialize

    def _initialize_with_eventlet_patch(*args, **kwargs):
        try_patch()
        return original(*args, **kwargs)

    auto_instrumentation_module._initialize = _initialize_with_eventlet_patch
