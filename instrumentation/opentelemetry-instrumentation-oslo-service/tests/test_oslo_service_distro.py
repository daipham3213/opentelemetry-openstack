"""Distro/configurator selection for oslo.service.

Eventlet monkey-patching itself now happens at interpreter startup, via
``sitecustomize`` calling into
``opentelemetry.instrumentation.auto_instrumentation._oslo_service_eventlet``
(see that module's tests for the patch-timing contract). The distro and
configurator here no longer apply the patch themselves; they only need to
wire the custom configurator into auto-instrumentation and stay loadable via
their entry points.
"""

import os
from importlib.metadata import entry_points
from unittest import mock

import pytest

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro
from opentelemetry.instrumentation.oslo_service.distro import (
    OsloServiceConfigurator,
    OsloServiceDistro,
)

_MANAGED_ENV = ("OTEL_PYTHON_CONFIGURATOR",)


@pytest.fixture(autouse=True)
def restore_env():
    saved = {k: os.environ.get(k) for k in _MANAGED_ENV}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_distro_selects_configurator():
    with mock.patch.object(OpenTelemetryDistro, "_configure"):
        OsloServiceDistro()._configure()
    assert os.environ["OTEL_PYTHON_CONFIGURATOR"] == "oslo_service"


def test_configurator_defers_to_super():
    with mock.patch.object(
        OpenTelemetryConfigurator, "_configure"
    ) as super_configure:
        OsloServiceConfigurator()._configure(foo="bar")
    super_configure.assert_called_once_with(foo="bar")


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
