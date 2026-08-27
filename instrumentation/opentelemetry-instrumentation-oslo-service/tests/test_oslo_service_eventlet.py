"""Patch-timing contract for the eventlet monkey-patch shim.

``eventlet.monkey_patch()`` only takes full effect if it runs before ``socket``
/ ``ssl`` are imported. ``sitecustomize`` covers that at interpreter startup by
calling :func:`try_patch` before importing ``auto_instrumentation`` at all,
then :func:`register_forkhook` and :func:`unpatch_sdk` once it is imported and
before ``initialize()`` builds the span processors. These tests pin each piece;
``test_oslo_service_distro`` pins the wiring that orders them.
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


@pytest.fixture(autouse=True)
def fork_registrations(monkeypatch):
    """``os.register_at_fork`` cannot be undone, so keep the real one out of the
    test process and let each test start from "not yet registered"."""
    monkeypatch.setattr(_oslo_service_eventlet, "_forkhook_installed", False)
    registrations = []
    monkeypatch.setattr(
        _oslo_service_eventlet.os,
        "register_at_fork",
        lambda **kw: registrations.append(kw),
    )
    return registrations


def test_register_forkhook_installs_a_child_handler(
    fake_eventlet, fork_registrations
):
    assert _oslo_service_eventlet.register_forkhook() is True
    assert len(fork_registrations) == 1
    assert set(fork_registrations[0]) == {"after_in_child"}


def test_register_forkhook_installs_only_once(
    fake_eventlet, fork_registrations
):
    """``os.register_at_fork`` has no unregister, so a second call must be a
    no-op rather than stacking another handler."""
    _oslo_service_eventlet.register_forkhook()
    assert _oslo_service_eventlet.register_forkhook() is True
    assert len(fork_registrations) == 1


def test_hub_reset_drops_an_inherited_hub(fake_eventlet, fork_registrations):
    _oslo_service_eventlet.register_forkhook()
    handler = fork_registrations[0]["after_in_child"]

    handler()

    fake_eventlet.hubs.use_hub.assert_called_once_with()


def test_hub_reset_leaves_a_child_without_a_live_hub_alone(
    fake_eventlet, fork_registrations
):
    """Inheriting eventlet is not the same as inheriting a running hub."""
    _oslo_service_eventlet.register_forkhook()
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
    assert _oslo_service_eventlet.register_forkhook() is True

    fork_registrations[0]["after_in_child"]()  # must not raise or import

    assert "eventlet" not in sys.modules


def test_hub_reset_never_raises_inside_fork(fake_eventlet, fork_registrations):
    """It runs inside os.fork(), in a child that may be about to exec."""
    _oslo_service_eventlet.register_forkhook()
    handler = fork_registrations[0]["after_in_child"]
    fake_eventlet.hubs.use_hub.side_effect = RuntimeError("no hub for you")

    handler()  # must not propagate


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


_SDK_NAMES = ("Event", "Lock", "RLock", "Thread")


@pytest.fixture
def sdk_modules():
    """The two SDK modules that own a worker thread other threads signal.

    Importing them for real pins the dotted paths and attribute names
    :func:`unpatch_sdk` rebinds, which is the part that rots silently when the
    SDK is upgraded.
    """
    from opentelemetry.sdk import _shared_internal  # noqa: PLC0415
    from opentelemetry.sdk.metrics._internal import export  # noqa: PLC0415

    saved_threading = _shared_internal.threading
    saved = {name: getattr(export, name) for name in _SDK_NAMES}
    yield _shared_internal, export
    _shared_internal.threading = saved_threading
    for name, value in saved.items():
        setattr(export, name, value)


@pytest.fixture
def sentinel_threading(fake_eventlet):
    """Stand-in for the unpatched threading, distinguishable by identity."""
    module = types.ModuleType("threading_original")
    for name in _SDK_NAMES:
        setattr(module, name, type(name, (), {}))
    fake_eventlet.patcher.original.return_value = module
    return module


def test_unpatch_sdk_rebinds_to_unpatched_threading(
    sdk_modules, sentinel_threading
):
    shared, export = sdk_modules

    assert _oslo_service_eventlet.unpatch_sdk() is True

    # BatchProcessor takes Thread, Lock and Event off the module, so the whole
    # module is rebound: a green Event signalled by a real thread is the bug.
    assert shared.threading is sentinel_threading
    for name in _SDK_NAMES:
        assert getattr(export, name) is getattr(sentinel_threading, name)


def test_unpatch_sdk_declines_a_non_module(fake_eventlet, sdk_modules):
    """A mocked eventlet returns a Mock; writing that in breaks every exporter."""
    shared, _export = sdk_modules
    before = shared.threading
    fake_eventlet.patcher.original.return_value = mock.MagicMock()

    assert _oslo_service_eventlet.unpatch_sdk() is False
    assert shared.threading is before


def test_unpatch_sdk_no_op_without_eventlet(monkeypatch, sdk_modules):
    shared, _export = sdk_modules
    before = shared.threading
    monkeypatch.setitem(sys.modules, "eventlet", None)

    assert _oslo_service_eventlet.unpatch_sdk() is False
    assert shared.threading is before


def test_try_patch_installs_the_follow_ups_when_opted_in(
    monkeypatch,
    fake_eventlet,
    fork_registrations,
    sdk_modules,
    sentinel_threading,
):
    shared, _export = sdk_modules
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )

    _oslo_service_eventlet.try_patch()

    assert len(fork_registrations) == 1
    assert shared.threading is sentinel_threading


def test_try_patch_leaves_the_process_alone_without_opt_in(
    monkeypatch,
    fake_eventlet,
    fork_registrations,
    sdk_modules,
    sentinel_threading,
):
    """Opting out means opting out of all of it, not just the monkey-patch."""
    shared, _export = sdk_modules
    before = shared.threading
    monkeypatch.delenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH,
        raising=False,
    )

    _oslo_service_eventlet.try_patch()

    assert fork_registrations == []
    assert shared.threading is before


def test_try_patch_skips_the_follow_ups_when_eventlet_is_missing(
    monkeypatch, fork_registrations, sdk_modules
):
    shared, _export = sdk_modules
    before = shared.threading
    monkeypatch.setenv(
        _oslo_service_eventlet.OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true"
    )
    monkeypatch.setitem(sys.modules, "eventlet", None)

    with pytest.warns(UserWarning, match="eventlet is not installed"):
        _oslo_service_eventlet.try_patch()

    assert fork_registrations == []
    assert shared.threading is before
