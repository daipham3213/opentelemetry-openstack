import os
import warnings

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro


class OsloServiceConfigurator(OpenTelemetryConfigurator):
    def _configure(self, **kwargs):
        super()._configure(**kwargs)

        eventlet_patch = os.environ.get(
            "OTEL_PYTHON_EVENTLET_MONKEY_PATCH", "false"
        )
        if eventlet_patch.lower() != "true":
            return

        try:
            eventlet = __import__("eventlet")
            eventlet.monkey_patch()

            # NOTE: This is a workaround to ensure that the oslo_service
            #  backend is used when the distro is loaded.
            os.environ["OTEL_PYTHON_OSLO_SERVICE_BACKEND"] = "eventlet"
        except ImportError:
            warnings.warn(
                "Eventlet is not installed, but OTEL_PYTHON_EVENTLET_MONKEY_PATCH is set to true."
            )


class OsloServiceDistro(OpenTelemetryDistro):
    """
    The OpenTelemetry provided Distro configures a default set of
    configuration out of the box.
    """

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

        # NOTE: This is a workaround to ensure that the oslo_service
        #  configurator is used when the distro is loaded.
        os.environ["OTEL_PYTHON_CONFIGURATOR"] = "oslo_service"
