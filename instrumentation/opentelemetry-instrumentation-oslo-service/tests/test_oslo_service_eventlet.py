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

_THIS_DIR = _AUTO_INSTRUMENTATION_DIR

_MANAGED_ENV = (
    "PYTHONPATH",
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
    # The fork handler resolves the hub from sys.modules rather than importing
    # it, so the submodule has to be registered the way a real import would.
    monkeypatch.setitem(sys.modules, "eventlet.hubs", module.hubs)
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


@pytest.fixture(autouse=True)
def fork_registrations(monkeypatch):
    """``os.register_at_fork`` cannot be undone, so keep the real one out of the
    test process and let each test start from "not yet registered"."""
    monkeypatch.setattr(_oslo_service_eventlet, "_hub_reset_installed", False)
    registrations = []
    monkeypatch.setattr(
        _oslo_service_eventlet.os,
        "register_at_fork",
        lambda **kw: registrations.append(kw),
    )
    return registrations


def test_reset_hub_on_fork_installs_a_child_handler(
    fake_eventlet, fork_registrations
):
    assert _oslo_service_eventlet.reset_hub_on_fork() is True
    assert len(fork_registrations) == 1
    assert set(fork_registrations[0]) == {"after_in_child"}


def test_reset_hub_on_fork_installs_only_once(
    fake_eventlet, fork_registrations
):
    """``os.register_at_fork`` has no unregister, so a second call must be a
    no-op rather than stacking another handler."""
    _oslo_service_eventlet.reset_hub_on_fork()
    assert _oslo_service_eventlet.reset_hub_on_fork() is True
    assert len(fork_registrations) == 1


def test_hub_reset_drops_an_inherited_hub(fake_eventlet, fork_registrations):
    _oslo_service_eventlet.reset_hub_on_fork()
    handler = fork_registrations[0]["after_in_child"]

    handler()

    fake_eventlet.hubs.use_hub.assert_called_once_with()


def test_hub_reset_leaves_a_child_without_a_live_hub_alone(
    fake_eventlet, fork_registrations
):
    """Inheriting eventlet is not the same as inheriting a running hub."""
    _oslo_service_eventlet.reset_hub_on_fork()
    handler = fork_registrations[0]["after_in_child"]
    fake_eventlet.hubs._threadlocal = types.SimpleNamespace(hub=None)

    handler()

    fake_eventlet.hubs.use_hub.assert_not_called()


def test_hub_reset_is_free_when_eventlet_was_never_imported(
    monkeypatch, fork_registrations
):
    """The common case for a non-OpenStack process: no eventlet, no cost."""
    monkeypatch.delitem(sys.modules, "eventlet", raising=False)
    monkeypatch.delitem(sys.modules, "eventlet.hubs", raising=False)
    assert _oslo_service_eventlet.reset_hub_on_fork() is True

    fork_registrations[0]["after_in_child"]()  # must not raise or import

    assert "eventlet" not in sys.modules


def test_hub_reset_never_raises_inside_fork(fake_eventlet, fork_registrations):
    """It runs inside os.fork(), in a child that may be about to exec."""
    _oslo_service_eventlet.reset_hub_on_fork()
    handler = fork_registrations[0]["after_in_child"]
    fake_eventlet.hubs.use_hub.side_effect = RuntimeError("no hub for you")

    handler()  # must not propagate


@pytest.mark.parametrize(("opted_in", "expected"), [("true", 1), ("false", 0)])
def test_initialize_installs_hub_reset_with_the_patch(
    monkeypatch, fake_eventlet, fork_registrations, opted_in, expected
):
    """It rides along with the monkey-patch: no patch from us, no handler."""
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, opted_in
    )
    module = types.SimpleNamespace(_initialize=mock.Mock())
    _oslo_service_eventlet.wrap_initialize(module)

    module._initialize()

    assert len(fork_registrations) == expected


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )


def _pythonpath_seen_by(fake_eventlet):
    """PYTHONPATH as it stood while eventlet was being patched."""
    seen = {}
    fake_eventlet.monkey_patch.side_effect = lambda: seen.update(
        value=os.environ.get("PYTHONPATH")
    )
    return seen


@pytest.mark.parametrize(
    "path",
    [
        # Trailing is the case a regex with a (?!$) guard silently misses.
        _THIS_DIR,
        _THIS_DIR + os.pathsep + "/opt/other",
        "/opt/other" + os.pathsep + _THIS_DIR,
        "/a" + os.pathsep + _THIS_DIR + os.pathsep + "/b",
    ],
)
def test_this_dir_is_hidden_while_patching(
    monkeypatch, fake_eventlet, opted_in, path
):
    """Anything spawned during the patch must not re-run auto-instrumentation."""
    monkeypatch.setenv("PYTHONPATH", path)
    seen = _pythonpath_seen_by(fake_eventlet)

    _oslo_service_eventlet.try_patch()

    assert _THIS_DIR not in (seen["value"] or "").split(os.pathsep)


def test_pythonpath_is_restored_exactly(monkeypatch, fake_eventlet, opted_in):
    path = "/a" + os.pathsep + _THIS_DIR + os.pathsep + "/b"
    monkeypatch.setenv("PYTHONPATH", path)

    _oslo_service_eventlet.try_patch()

    assert os.environ["PYTHONPATH"] == path


def test_pythonpath_restored_even_when_eventlet_is_missing(
    monkeypatch, opted_in
):
    path = _THIS_DIR + os.pathsep + "/opt/other"
    monkeypatch.setenv("PYTHONPATH", path)
    monkeypatch.setitem(sys.modules, "eventlet", None)

    with pytest.warns(UserWarning):
        _oslo_service_eventlet.try_patch()

    assert os.environ["PYTHONPATH"] == path


def test_unset_pythonpath_stays_unset(monkeypatch, fake_eventlet, opted_in):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    _oslo_service_eventlet.try_patch()

    assert "PYTHONPATH" not in os.environ
