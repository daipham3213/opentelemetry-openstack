"""OpenTelemetry auto-instrumentation distro and configurator for oslo.service.

Under ``opentelemetry-instrument`` this module supplies the distro and
configurator that enable :class:`OsloServiceInstrumentor` with no code change
and -- when opted in via ``OTEL_PYTHON_EVENTLET_MONKEY_PATCH`` -- apply
eventlet's stdlib monkey-patch at the earliest possible moment.

Timing is the whole point. ``eventlet.monkey_patch()`` only takes full effect if
it runs *before* ``socket`` / ``ssl`` / ``select`` are imported; the OTLP
exporter pulls that stack in the moment the SDK is configured (inside
``OpenTelemetry{Distro,Configurator}._configure``). A partial patch leaves
eventlet not owning the RabbitMQ socket, so a connection opened by a
``fork()``-ing service (oslo.service ``ProcessLauncher``, e.g. multi-backend
``cinder-volume``) is inherited across the fork and shared between parent and
child -- oslo.messaging logs "Process forked after connection established!" and
message ACKs are lost.

The patch is therefore applied from the distro (the earliest hook
auto-instrumentation runs, before any exporter import) *and* from the
configurator before its own ``super()._configure()``, so a stock-distro setup is
still covered. ``eventlet.monkey_patch()`` is idempotent, so patching from both
places is harmless. A missing ``eventlet`` is surfaced with
:func:`warnings.warn` rather than the module logger on purpose: this runs during
startup, before the logging pipeline is configured, so a warning is the reliably
visible channel for the misconfiguration.
"""

import logging
import os
import warnings

from opentelemetry.distro import OpenTelemetryConfigurator, OpenTelemetryDistro

__all__ = [
    "OsloServiceConfigurator",
    "OsloServiceDistro",
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
]

_LOG = logging.getLogger(__name__)

#: Opt-in flag: when ``"true"`` (case-insensitive), eventlet is monkey-patched.
OTEL_PYTHON_EVENTLET_MONKEY_PATCH = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

#: Backend forced for oslo.service once eventlet has been patched.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"


def _try_patch():
    flag = os.environ.get(OTEL_PYTHON_EVENTLET_MONKEY_PATCH) or "false"
    if flag.strip().lower() != "true":
        return

    try:
        eventlet = __import__("eventlet")
    except ImportError:
        warnings.warn(
            f"{OTEL_PYTHON_EVENTLET_MONKEY_PATCH} is set to true, but "
            "eventlet is not installed; continuing without "
            "monkey-patching.",
            stacklevel=2,
        )
    else:
        eventlet.monkey_patch()
        # Forced (not ``setdefault``): a stale threading value would
        # re-introduce the fork/socket mismatch the patch prevents.
        os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
        _LOG.debug(
            "eventlet monkey-patch applied; pinned %s=eventlet",
            OTEL_PYTHON_OSLO_SERVICE_BACKEND,
        )


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
        _try_patch()

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
        _try_patch()

        super()._configure(**kwargs)

        # Ensure this distro's own configurator (which patches defensively too)
        # is the one auto-instrumentation loads.
        os.environ["OTEL_PYTHON_CONFIGURATOR"] = "oslo_service"
