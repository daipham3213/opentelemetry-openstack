"""OpenTelemetry instrumentation for TaskFlow.

`TaskFlow <https://docs.openstack.org/taskflow/>`_ runs *flows* of *atoms*
(:class:`~taskflow.task.Task` and :class:`~taskflow.retry.Retry` objects) on an
*engine*. This instrumentor records each engine execution as a root
``taskflow.flow.run`` span and each atom ``execute``/``revert`` call as a child
span, giving a single trace that ties an entire flow together.

Rather than patching atom methods, the instrumentor uses TaskFlow's native
notification API: it patches :meth:`taskflow.engines.base.Engine.__init__` so
that every engine -- however it is constructed (``engines.run``,
``engines.load``, a factory, or directly) -- gets an OpenTelemetry listener
attached to its flow and atom notifiers. See
:mod:`opentelemetry.instrumentation.taskflow.listener` for the listener itself.

Usage::

    from opentelemetry.instrumentation.taskflow import TaskflowInstrumentor

    TaskflowInstrumentor().instrument()
"""

import logging
from functools import wraps
from importlib import import_module
from typing import Collection

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.taskflow.version import __version__

_LOG = logging.getLogger(__name__)

_instruments = ("taskflow",)


def _wrap_engine_init(original, tracer):
    """Wrap ``Engine.__init__`` to attach a trace listener to each engine."""
    # Imported lazily so the package imports cleanly when taskflow is absent
    # (the listener module imports taskflow at its top level).
    listener = import_module("opentelemetry.instrumentation.taskflow.listener")

    @wraps(original)
    def traced_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            listener.attach(self, tracer)
        except Exception:
            _LOG.warning(
                "Failed to attach OpenTelemetry listener to TaskFlow engine "
                "%r; it will not be traced",
                self,
                exc_info=True,
            )

    return traced_init


class TaskflowInstrumentor(BaseInstrumentor):
    """An instrumentor for TaskFlow engines."""

    _original_engine_init = None

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        if TaskflowInstrumentor._original_engine_init is not None:
            return  # already instrumented

        try:
            from taskflow.engines import base as engine_base  # noqa: PLC0415
        except ImportError:
            return  # taskflow is not available

        tracer_provider = kwargs.get("tracer_provider")
        tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider=tracer_provider,
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

        TaskflowInstrumentor._original_engine_init = (
            engine_base.Engine.__init__
        )
        engine_base.Engine.__init__ = _wrap_engine_init(
            TaskflowInstrumentor._original_engine_init,
            tracer,
        )

    def _uninstrument(self, **kwargs):
        if TaskflowInstrumentor._original_engine_init is None:
            return

        from taskflow.engines import base as engine_base  # noqa: PLC0415

        engine_base.Engine.__init__ = (
            TaskflowInstrumentor._original_engine_init
        )
        TaskflowInstrumentor._original_engine_init = None


__all__ = ["TaskflowInstrumentor"]
