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
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path
from unittest import mock

import pytest

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro
from opentelemetry.instrumentation.oslo_service.distro import (
    OsloServiceConfigurator,
    OsloServiceDistro,
)

_MANAGED_ENV = ("OTEL_PYTHON_CONFIGURATOR", "OTEL_EXPORTER_OTLP_PROTOCOL")


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


def test_distro_defaults_otlp_protocol_to_http():
    # The artifact ships only the HTTP exporter, but the upstream distro
    # defaults the protocol to grpc -- leaving it there means the SDK asks for
    # an exporter that is not installed and the process exports nothing.
    os.environ.pop("OTEL_EXPORTER_OTLP_PROTOCOL", None)

    with mock.patch.object(OpenTelemetryDistro, "_configure"):
        OsloServiceDistro()._configure()

    assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"


def test_distro_keeps_an_explicit_otlp_protocol():
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"

    with mock.patch.object(OpenTelemetryDistro, "_configure"):
        OsloServiceDistro()._configure()

    assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"


def test_distro_default_survives_the_real_upstream_configure():
    # Both this distro and the upstream one use setdefault, so ordering is what
    # makes ours win. Exercise it without mocking super().
    os.environ.pop("OTEL_EXPORTER_OTLP_PROTOCOL", None)

    OsloServiceDistro()._configure()

    assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"


_AUTO_INSTRUMENTATION_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "opentelemetry"
    / "instrumentation"
    / "auto_instrumentation"
)


#: Both follow-ups ride on the monkey-patch opt-in.
_OPTED_IN = {"OTEL_PYTHON_EVENTLET_MONKEY_PATCH": "true"}

_REPORT_DISTRO = "import os; print(os.environ['OTEL_PYTHON_DISTRO'])"


def _run_sitecustomize(env_overrides, code=_REPORT_DISTRO):
    """Run ``code`` in a fresh interpreter with ``sitecustomize`` active.

    ``sitecustomize`` configures the SDK at import, so it cannot be imported
    into the test process; a subprocess is the only honest way to exercise it.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(_AUTO_INSTRUMENTATION_DIR),
        # No exporter packages needed to observe the distro selection.
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        **env_overrides,
    }
    env.pop("OTEL_PYTHON_DISTRO", None)
    env.update(env_overrides)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip().splitlines()[-1]


def test_sitecustomize_selects_this_distro():
    # _load_distro takes the *first* opentelemetry_distro entry point when
    # OTEL_PYTHON_DISTRO is unset, and upstream's "distro" sorts ahead of
    # "oslo_service" -- so without this the OpenStack distro never runs, and
    # neither does the configurator it pins nor its OTLP protocol default.
    assert _run_sitecustomize({}) == "oslo_service"


def test_sitecustomize_respects_an_explicit_distro():
    assert _run_sitecustomize({"OTEL_PYTHON_DISTRO": "distro"}) == "distro"


def test_sitecustomize_gives_the_sdk_real_threads():
    """The SDK's exporter worker is signalled by whichever thread ends a span.

    Under mod_wsgi that is a different real thread every request, and a green
    Event signalled from a foreign thread raises ``greenlet.error: cannot
    switch to a different thread``. sitecustomize has to rebind the module
    before ``initialize()`` builds the processors.
    """
    reported = _run_sitecustomize(
        _OPTED_IN,
        "import sys\n"
        "from eventlet import patcher\n"
        "module = sys.modules['opentelemetry.sdk._shared_internal']\n"
        "print(module.threading is patcher.original('threading'))\n",
    )
    assert reported == "True"


def test_sitecustomize_registers_the_fork_hub_reset():
    """Forked children must not inherit the parent's hub; see
    ``_oslo_service_eventlet.register_forkhook``."""
    reported = _run_sitecustomize(
        _OPTED_IN,
        "import _oslo_service_eventlet as m\nprint(m._forkhook_installed)\n",
    )
    assert reported == "True"
