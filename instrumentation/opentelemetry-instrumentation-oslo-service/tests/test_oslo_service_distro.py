"""Eventlet monkey-patch timing for the distro and configurator.

``eventlet.monkey_patch()`` only takes full effect if it runs before the socket
stack is imported. The OTLP exporter imports that stack inside
``OpenTelemetry{Distro,Configurator}._configure``, so the patch must happen
*before* the ``super()._configure()`` call. A partial patch lets a RabbitMQ
connection be inherited across an oslo.service ``ProcessLauncher`` fork, which
breaks message acking -- these tests pin the ordering so that cannot regress.
"""

import os
import sys
from importlib.metadata import entry_points
from unittest import mock

import pytest

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro
from opentelemetry.instrumentation.oslo_service.distro import (
    OTEL_PYTHON_EVENTLET_MONKEY_PATCH,
    OTEL_PYTHON_OSLO_SERVICE_BACKEND,
    OsloServiceConfigurator,
    OsloServiceDistro,
)

_MANAGED_ENV = (
    OTEL_PYTHON_EVENTLET_MONKEY_PATCH,
    OTEL_PYTHON_OSLO_SERVICE_BACKEND,
    "OTEL_PYTHON_CONFIGURATOR",
)


@pytest.fixture(autouse=True)
def restore_env():
    # ``_maybe_monkey_patch_eventlet`` writes os.environ directly (not via
    # monkeypatch), so snapshot and restore the keys it touches.
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


@pytest.mark.parametrize(
    "cls, super_cls",
    [
        (OsloServiceDistro, OpenTelemetryDistro),
        (OsloServiceConfigurator, OpenTelemetryConfigurator),
    ],
)
def test_patches_eventlet_before_super(
    cls, super_cls, monkeypatch, fake_eventlet
):
    monkeypatch.setenv(OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true")

    manager = mock.Mock()
    manager.attach_mock(fake_eventlet.monkey_patch, "monkey_patch")
    with mock.patch.object(super_cls, "_configure") as super_configure:
        manager.attach_mock(super_configure, "super_configure")
        cls()._configure()

    called = [name for name, _, _ in manager.mock_calls]
    assert "monkey_patch" in called
    assert called.index("monkey_patch") < called.index("super_configure")
    # A green stack must run oslo.service on its eventlet backend.
    assert os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] == "eventlet"


def test_distro_selects_configurator(monkeypatch, fake_eventlet):
    monkeypatch.setenv(OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true")
    with mock.patch.object(OpenTelemetryDistro, "_configure"):
        OsloServiceDistro()._configure()
    assert os.environ["OTEL_PYTHON_CONFIGURATOR"] == "oslo_service"


@pytest.mark.parametrize(
    "cls, super_cls",
    [
        (OsloServiceDistro, OpenTelemetryDistro),
        (OsloServiceConfigurator, OpenTelemetryConfigurator),
    ],
)
def test_no_patch_without_opt_in(cls, super_cls, monkeypatch, fake_eventlet):
    monkeypatch.delenv(OTEL_PYTHON_EVENTLET_MONKEY_PATCH, raising=False)
    with mock.patch.object(super_cls, "_configure"):
        cls()._configure()
    fake_eventlet.monkey_patch.assert_not_called()
    assert OTEL_PYTHON_OSLO_SERVICE_BACKEND not in os.environ


def test_warns_when_eventlet_missing(monkeypatch):
    monkeypatch.setenv(OTEL_PYTHON_EVENTLET_MONKEY_PATCH, "true")
    # A None entry in sys.modules makes ``import eventlet`` raise ImportError.
    monkeypatch.setitem(sys.modules, "eventlet", None)
    with mock.patch.object(OpenTelemetryDistro, "_configure"):
        with pytest.warns(UserWarning, match="eventlet is not installed"):
            OsloServiceDistro()._configure()
    assert OTEL_PYTHON_OSLO_SERVICE_BACKEND not in os.environ


@pytest.mark.parametrize(
    "value, patched",
    [
        ("true", True),
        ("TRUE", True),
        ("  True  ", True),
        ("false", False),
        ("1", False),
        ("yes", False),
        ("", False),
    ],
)
def test_opt_in_flag_parsing(value, patched, monkeypatch, fake_eventlet):
    # Exercise the env parsing through the public _configure surface now that
    # the check is inlined (no helper to call directly).
    monkeypatch.setenv(OTEL_PYTHON_EVENTLET_MONKEY_PATCH, value)
    with mock.patch.object(OpenTelemetryDistro, "_configure"):
        OsloServiceDistro()._configure()
    assert fake_eventlet.monkey_patch.called is patched


@pytest.mark.parametrize(
    "group, value",
    [
        (
            "opentelemetry_distro",
            "opentelemetry.instrumentation.oslo_service.distro:OsloServiceDistro",
        ),
        (
            "opentelemetry_configurator",
            "opentelemetry.instrumentation.oslo_service.distro:OsloServiceConfigurator",
        ),
    ],
)
def test_entry_point_registered_and_loadable(group, value):
    matches = [
        ep for ep in entry_points(group=group) if ep.name == "oslo_service"
    ]
    # Assert the full module path (not just the class suffix) so a stale
    # registration pointing at an old location fails loudly.
    assert [ep.value for ep in matches] == [value]
    # And that the path actually resolves to the class.
    assert matches[0].load().__name__ == value.rsplit(":", 1)[1]
