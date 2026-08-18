"""OpenTelemetry instrumentation distro and configurator for oslo.service."""

import os

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro
from opentelemetry.instrumentation._oslo_service_eventlet import (
    OTEL_PYTHON_EVENTLET_MONKEY_PATCH,
    OTEL_PYTHON_OSLO_SERVICE_BACKEND,
    try_patch,
)

__all__ = [
    "OsloServiceConfigurator",
    "OsloServiceDistro",
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
]


class OsloServiceConfigurator(OpenTelemetryConfigurator):
    """Configurator that green-patches eventlet before configuring the SDK."""

    def _configure(self, **kwargs: object) -> None:
        """Apply the eventlet patch, then run the standard SDK configuration.

        The patch precedes ``super()._configure()`` because that call
        instantiates the OTLP exporter, which imports the raw socket stack. This
        mirrors :class:`OsloServiceDistro` so the configurator stays correct when
        paired with the stock ``opentelemetry-distro``.

        :param kwargs: Forwarded verbatim to
            :meth:`OpenTelemetryConfigurator._configure`.
        :returns: ``None``.
        """
        try_patch()

        super()._configure(**kwargs)


class OsloServiceDistro(OpenTelemetryDistro):
    """Distro that green-patches eventlet at the earliest available hook.

    Auto-instrumentation loads the distro before any configurator, making this
    the earliest in-process point -- and the only one guaranteed to run before
    an exporter imports ``socket``.
    """

    def _configure(self, **kwargs: object) -> None:
        """Patch eventlet, run standard distro config, then pin the configurator.

        :param kwargs: Forwarded verbatim to
            :meth:`OpenTelemetryDistro._configure`.
        :returns: ``None``.
        """
        try_patch()

        super()._configure(**kwargs)

        # Ensure this distro's own configurator (which patches defensively too)
        # is the one auto-instrumentation loads.
        os.environ["OTEL_PYTHON_CONFIGURATOR"] = "oslo_service"
