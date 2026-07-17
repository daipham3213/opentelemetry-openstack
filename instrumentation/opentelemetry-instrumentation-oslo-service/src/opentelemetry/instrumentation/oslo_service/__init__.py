"""OpenTelemetry instrumentation for oslo.service.

oslo.service is OpenStack's service framework: it launches services and runs
their background work (``ThreadGroup``, looping/periodic calls, RPC servers) on
either a *threading* or an *eventlet* backend. This instrumentor does not emit
telemetry of its own -- it makes the trace context *survive* the concurrency
boundaries oslo.service introduces, so spans and oslo.log records created in
spawned work stay on the originating request's trace.

On ``instrument()`` it:

* selects the oslo.service backend (threading by default) unless the host has
  already chosen one -- controlled by ``OTEL_PYTHON_OSLO_SERVICE_BACKEND`` or the
  ``backend`` keyword;
* propagates the active OpenTelemetry context across oslo.service's concurrency
  primitives -- native ``threading.Thread`` workers (``ThreadGroup.add_thread``),
  ``futurist`` thread pools (looping/periodic calls) and, when eventlet is
  present, ``eventlet``/``GreenPool`` spawns (see :mod:`._propagation`).

This propagation used to live in the oslo.messaging (eventlet spawns) and
oslo.log (threads/futurist) instrumentors; it is consolidated here so it is not
duplicated and is available to every OpenStack service, whichever of those it
instruments.

Usage::

    from opentelemetry.instrumentation.oslo_service import OsloServiceInstrumentor

    OsloServiceInstrumentor().instrument()
"""

import logging
from os import environ
from typing import Collection, Optional

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.oslo_service._propagation import (
    install,
    uninstall,
)
from opentelemetry.instrumentation.oslo_service.version import __version__

_LOG = logging.getLogger(__name__)

_instruments = ("oslo.service",)

#: Environment variable selecting the oslo.service backend to initialize.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"

try:
    from oslo_service import backend as _oslo_service_backend

    _BACKEND_BY_NAME = {
        "threading": _oslo_service_backend.BackendType.THREADING,
        "eventlet": _oslo_service_backend.BackendType.EVENTLET,
    }
except ImportError:  # pragma: no cover - oslo.service backends (>= 4.1.0)
    _oslo_service_backend = None
    _BACKEND_BY_NAME = {}


def _select_backend(requested: Optional[str]) -> None:
    """Initialize the oslo.service backend, respecting any existing choice.

    ``init_backend`` raises if a *different* backend is already active, and a
    backend gets selected as a side effect of importing parts of oslo.service /
    oslo.messaging (it defaults to eventlet), so blindly initializing would break
    a host that imported those first. We only select when nothing has been
    chosen yet, and otherwise leave the host's choice untouched.
    """
    if _oslo_service_backend is None:
        _LOG.debug(
            "oslo_service.backend unavailable (oslo.service < 4.1.0); "
            "skipping backend selection - context propagation still active"
        )
        return

    name = requested or environ.get(
        OTEL_PYTHON_OSLO_SERVICE_BACKEND, "threading"
    )
    backend_type = _BACKEND_BY_NAME.get(name)
    if backend_type is None:
        _LOG.warning(
            "Unknown oslo_service backend %r requested; expected one of %s. "
            "Leaving backend selection to oslo.service.",
            name,
            sorted(_BACKEND_BY_NAME),
        )
        return

    current = _oslo_service_backend.get_backend_type()
    if current is None:
        try:
            _oslo_service_backend.init_backend(backend_type)
        except Exception:  # pragma: no cover - backend deps may be missing
            # e.g. the threading backend needs ``cotyledon``; a failure to
            # initialize must not break instrumenting the host application.
            _LOG.debug(
                "Failed to initialize the oslo_service %r backend; leaving "
                "selection to oslo.service",
                backend_type.value,
                exc_info=True,
            )
    elif current != backend_type:
        _LOG.debug(
            "oslo_service backend already set to %r; leaving it unchanged "
            "(requested %r)",
            current.value,
            backend_type.value,
        )


def _pre_instrument() -> None:
    """Select the oslo.service backend before any instrumentor is loaded.

    Registered as an ``opentelemetry_pre_instrument`` hook. Auto-instrumentation
    runs those hooks *before* it loads any instrumentor, hence before an
    instrumentor imports its target library (oslo.messaging, taskflow, ...) and
    that import lazily resolves oslo.service's backend to its eventlet default.

    ``OsloServiceInstrumentor._instrument`` also selects the backend, but only
    wins if it happens to run before any such library import -- which it cannot
    guarantee, since instrumentor load order is not controlled. Selecting from a
    pre-instrument hook removes that race: the threading default (or whatever
    ``OTEL_PYTHON_OSLO_SERVICE_BACKEND`` requests) is locked in first, and every
    later import simply sees the already-chosen backend.
    """
    _select_backend(None)


class OsloServiceInstrumentor(BaseInstrumentor):
    """Propagate trace context across oslo.service's concurrency primitives."""

    # Class-level default; assigned per run in ``_instrument``. Stored here
    # rather than in ``__init__`` because ``BaseInstrumentor`` is a singleton and
    # a re-running ``__init__`` would clobber live state.
    _wraps = ()

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        """Select the backend and wrap oslo.service's concurrency primitives.

        Args:
            backend: ``"threading"`` (default) or ``"eventlet"``; overrides the
                ``OTEL_PYTHON_OSLO_SERVICE_BACKEND`` env var. Ignored when a
                backend is already active or oslo.service is too old to expose
                one.
        """
        _select_backend(kwargs.get("backend"))
        self._wraps = install()

    def _uninstrument(self, **kwargs):
        uninstall(self._wraps)
        self._wraps = ()


__all__ = ["OsloServiceInstrumentor", "_pre_instrument", "__version__"]
