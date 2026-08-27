"""OpenTelemetry instrumentation distro and configurator for oslo.service."""

import os

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro
from opentelemetry.sdk.environment_variables import OTEL_EXPORTER_OTLP_PROTOCOL

__all__ = [
    "OsloServiceConfigurator",
    "OsloServiceDistro",
]

#: OTLP protocol this distro defaults to. The upstream distro defaults to
#: ``grpc``, whose exporter is deliberately not shipped in the
#: auto-instrumentation artifact (gRPC has a strict dependency on the OS and
#: Python version the artifact is built for). Left at the upstream default, the
#: SDK asks for an ``otlp_proto_grpc`` exporter that is not installed, fails to
#: configure, and the process exports nothing at all.
DEFAULT_OTLP_PROTOCOL = "http/protobuf"


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
        super()._configure(**kwargs)


class OsloServiceDistro(OpenTelemetryDistro):
    """Distro that green-patches eventlet at the earliest available hook.

    Auto-instrumentation loads the distro before any configurator, making this
    the earliest in-process point -- and the only one guaranteed to run before
    an exporter imports ``socket``.
    """

    def _configure(self, **kwargs: object) -> None:
        """Default the OTLP protocol, run distro config, pin the configurator.

        The protocol default is set *before* ``super()._configure()`` because
        that also uses ``setdefault`` -- whichever runs first wins. An operator
        who has installed the gRPC exporter and set
        ``OTEL_EXPORTER_OTLP_PROTOCOL`` explicitly still gets their choice.

        :param kwargs: Forwarded verbatim to
            :meth:`OpenTelemetryDistro._configure`.
        :returns: ``None``.
        """
        os.environ.setdefault(
            OTEL_EXPORTER_OTLP_PROTOCOL, DEFAULT_OTLP_PROTOCOL
        )

        super()._configure(**kwargs)

        # Ensure this distro's own configurator (which patches defensively too)
        # is the one auto-instrumentation loads.
        os.environ["OTEL_PYTHON_CONFIGURATOR"] = "oslo_service"
