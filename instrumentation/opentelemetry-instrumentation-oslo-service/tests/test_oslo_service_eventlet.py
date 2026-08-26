"""Patch-timing contract for the eventlet monkey-patch shim.

``eventlet.monkey_patch()`` only takes full effect if it runs before ``socket``
/ ``ssl`` are imported. ``sitecustomize`` covers that at interpreter startup by
calling :func:`try_patch` before importing ``auto_instrumentation`` at all; on
top of that, :func:`wrap_initialize` folds the patch into
``auto_instrumentation._initialize`` itself, so a later call to
``initialize()`` -- re-entrant, or made directly by application code -- still
re-applies it. These tests pin both contracts so they cannot regress.
"""

import os
import sys
import types
from unittest import mock

import pytest

# In production this module is loaded as a plain top-level module: the
# ``sitecustomize`` bootstrap adds this directory to ``PYTHONPATH`` so
# ``import _oslo_service_eventlet`` resolves before anything has pulled in the
# real ``opentelemetry.instrumentation.auto_instrumentation`` package (which
# would drag in the socket stack too early). Mirror that here rather than
# importing it dotted, since the dotted path only resolves once the real
# ``opentelemetry-instrumentation`` distribution's namespace package has been
# built alongside this one (i.e. after a real install, not in this editable,
# split-source-root dev environment).
_AUTO_INSTRUMENTATION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "opentelemetry",
    "instrumentation",
    "auto_instrumentation",
)
if _AUTO_INSTRUMENTATION_DIR not in sys.path:
    sys.path.insert(0, _AUTO_INSTRUMENTATION_DIR)

import _oslo_service_eventlet  # noqa: E402

_MANAGED_ENV = (
    _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH,
    _oslo_service_eventlet.OTEL_PYTHON_OSLO_SERVICE_BACKEND,
)


@pytest.fixture(autouse=True)
def restore_env():
    saved = {k: os.environ.get(k) for k in _MANAGED_ENV}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def fake_eventlet(monkeypatch):
    """A stand-in eventlet whose ``monkey_patch`` is a mock (never patches the
    real interpreter, which would corrupt the rest of the test run)."""
    module = mock.MagicMock(name="eventlet")
    monkeypatch.setitem(sys.modules, "eventlet", module)
    return module


def test_try_patch_no_op_without_opt_in(monkeypatch, fake_eventlet):
    monkeypatch.delenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH,
        raising=False,
    )
    _oslo_service_eventlet.try_patch()
    fake_eventlet.monkey_patch.assert_not_called()
    assert (
        _oslo_service_eventlet.OTEL_PYTHON_OSLO_SERVICE_BACKEND
        not in os.environ
    )


def test_try_patch_patches_when_opted_in(monkeypatch, fake_eventlet):
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )
    _oslo_service_eventlet.try_patch()
    fake_eventlet.monkey_patch.assert_called_once()
    assert (
        os.environ[_oslo_service_eventlet.OTEL_PYTHON_OSLO_SERVICE_BACKEND]
        == "eventlet"
    )


def test_try_patch_warns_when_eventlet_missing(monkeypatch):
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )
    # A None entry in sys.modules makes ``import eventlet`` raise ImportError.
    monkeypatch.setitem(sys.modules, "eventlet", None)
    with pytest.warns(UserWarning, match="eventlet is not installed"):
        _oslo_service_eventlet.try_patch()
    assert (
        _oslo_service_eventlet.OTEL_PYTHON_OSLO_SERVICE_BACKEND
        not in os.environ
    )


def test_wrap_initialize_patches_before_delegating(monkeypatch, fake_eventlet):
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )

    manager = mock.Mock()
    manager.attach_mock(fake_eventlet.monkey_patch, "monkey_patch")
    original = mock.Mock(name="_initialize")
    manager.attach_mock(original, "_initialize")

    module = types.SimpleNamespace(_initialize=original)
    _oslo_service_eventlet.wrap_initialize(module)

    module._initialize(swallow_exceptions=True)
    original.assert_called_once_with(swallow_exceptions=True)

    called = [name for name, _, _ in manager.mock_calls]
    assert called.index("monkey_patch") < called.index("_initialize")


def test_wrap_initialize_reapplies_patch_on_each_call(
    monkeypatch, fake_eventlet
):
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )

    module = types.SimpleNamespace(_initialize=mock.Mock())
    _oslo_service_eventlet.wrap_initialize(module)

    module._initialize()
    module._initialize()
    assert fake_eventlet.monkey_patch.call_count == 2
