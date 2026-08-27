"""Backend selection via the ``opentelemetry_pre_instrument`` hook.

oslo.service resolves its backend lazily -- defaulting to eventlet -- the first
time parts of it are imported, and caches the choice permanently. Under
auto-instrumentation an instrumentor's target library (oslo.messaging, taskflow,
...) can trigger that import before ``OsloServiceInstrumentor`` runs, locking in
eventlet so a threading host's ``ThreadGroup`` work never executes. ``_pre_instrument``
is registered as an ``opentelemetry_pre_instrument`` hook, which runs before any
instrumentor loads, so it wins that race.
"""

from importlib.metadata import entry_points

import pytest

from opentelemetry.instrumentation.oslo_service import _pre_instrument

backend = pytest.importorskip("oslo_service.backend")
BackendType = backend.BackendType


@pytest.fixture(autouse=True)
def reset_backend():
    backend._reset_backend()
    yield
    backend._reset_backend()


def test_pre_instrument_selects_threading_by_default():
    # Both are runtime deps of the threading backend; without either,
    # oslo.service reports a misleading "backend module not found".
    pytest.importorskip("cotyledon")
    pytest.importorskip("futurist")
    assert backend.get_backend_type() is None
    _pre_instrument()
    assert backend.get_backend_type() is BackendType.THREADING


def test_pre_instrument_respects_env_override(monkeypatch):
    monkeypatch.setenv("OTEL_PYTHON_OSLO_SERVICE_BACKEND", "eventlet")
    _pre_instrument()
    assert backend.get_backend_type() is BackendType.EVENTLET


def test_pre_instrument_leaves_existing_choice_untouched():
    # A host that already chose eventlet must not be overridden (and no raise).
    backend.init_backend(BackendType.EVENTLET)
    _pre_instrument()
    assert backend.get_backend_type() is BackendType.EVENTLET


def test_pre_instrument_ignores_unknown_backend(monkeypatch):
    monkeypatch.setenv("OTEL_PYTHON_OSLO_SERVICE_BACKEND", "bogus")
    _pre_instrument()
    # Unknown request: leave selection to oslo.service (nothing forced).
    assert backend.get_backend_type() is None


def test_pre_instrument_entry_point_registered():
    eps = entry_points(group="opentelemetry_pre_instrument")
    assert any(
        ep.name == "oslo_service" and ep.value.endswith(":_pre_instrument")
        for ep in eps
    )
