"""Eventlet monkey-patch, factored out so it can run at the earliest hook.

``eventlet.monkey_patch()`` only takes full effect if it runs *before*
``socket`` / ``ssl`` / ``select`` are imported (see the timing rationale in
:mod:`opentelemetry.instrumentation.oslo_service.distro`). Both the
auto-instrumentation ``sitecustomize`` bootstrap and the distro/configurator
need to apply that patch, so the logic lives here rather than being duplicated.

This module deliberately imports **only the standard library** and lives under
the lightweight ``opentelemetry.instrumentation`` namespace package: importing
it must not drag in the socket stack (or the ``oslo_service`` package, whose
``__init__`` imports ``eventlet``/``socket``), or the patch would already be too
late by the time :func:`try_patch` runs.
"""

import logging
import os
import warnings

__all__ = [
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
    "try_patch",
]

_LOG = logging.getLogger(__name__)

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
    except ImportError:
        warnings.warn(
            f"{OTEL_PYTHON_EVENTLET_MONKEY_PATCH} is set to true, but "
            "eventlet is not installed; continuing without "
            "monkey-patching.",
            stacklevel=2,
        )
    else:
        eventlet.monkey_patch()
        # Forced (not ``setdefault``): a stale threading value would
        # re-introduce the fork/socket mismatch the patch prevents.
        os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
        _LOG.debug(
            "eventlet monkey-patch applied; pinned %s=eventlet",
            OTEL_PYTHON_OSLO_SERVICE_BACKEND,
        )
