"""OpenTelemetry instrumentation for TaskFlow.

`TaskFlow <https://docs.openstack.org/taskflow/>`_ models work as *atoms* — most
commonly :class:`~taskflow.task.Task` objects that implement an ``execute``
method (to perform work) and an optional ``revert`` method (to roll it back when
a flow fails). This instrumentor records every ``execute``/``revert`` call as a
span named ``taskflow.task.<method>``, annotated with the task's class, method
and name.

Concrete tasks override ``execute``/``revert``, so patching the methods on
:class:`~taskflow.task.Task` itself would never see those overrides. Instead the
instrumentor wraps ``Task.__getattribute__`` so that accessing either method on
*any* Task instance returns a traced wrapper, regardless of where the method is
defined.

Usage::

    from opentelemetry.instrumentation.taskflow import TaskflowInstrumentor

    TaskflowInstrumentor().instrument()
"""

from functools import wraps
from typing import Collection

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.taskflow.version import __version__

try:
    from taskflow.task import Task
except ImportError:
    Task = None

_instruments = ("taskflow",)
_TRACED_METHODS = frozenset(("execute", "revert"))


def _task_attributes(task, method):
    """Build the span attributes describing ``task`` and the called ``method``."""
    task_type = type(task)
    attributes = {
        "taskflow.task.class": f"{task_type.__module__}.{task_type.__qualname__}",
        "taskflow.task.method": method,
    }

    task_name = getattr(task, "name", None)
    if task_name is not None:
        attributes["taskflow.task.name"] = task_name

    return attributes


def _wrap_task_method(task, method_name, method, tracer):
    """Wrap a bound task method so each call is recorded as a span."""

    @wraps(method)
    def traced_method(*args, **kwargs):
        with tracer.start_as_current_span(
            f"taskflow.task.{method_name}",
            attributes=_task_attributes(task, method_name),
            record_exception=True,
            set_status_on_exception=True,
        ):
            return method(*args, **kwargs)

    return traced_method


def _wrap_getattribute(getattribute, tracer):
    """Wrap ``Task.__getattribute__`` to trace traced-method lookups."""

    def traced_getattribute(self, name):
        attribute = getattribute(self, name)
        if name in _TRACED_METHODS and callable(attribute):
            return _wrap_task_method(self, name, attribute, tracer)
        return attribute

    return traced_getattribute


class TaskflowInstrumentor(BaseInstrumentor):
    """An instrumentor for TaskFlow task atoms.

    Args:
        tracer_provider: ``TracerProvider`` used to obtain the tracer. Defaults
            to the global provider.
    """

    _original_getattribute = None

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        if Task is None:
            return  # taskflow is not available

        if TaskflowInstrumentor._original_getattribute is not None:
            return  # already instrumented

        tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider=kwargs.get("tracer_provider"),
        )

        TaskflowInstrumentor._original_getattribute = Task.__getattribute__
        Task.__getattribute__ = _wrap_getattribute(
            TaskflowInstrumentor._original_getattribute,
            tracer,
        )

    def _uninstrument(self, **kwargs):
        if TaskflowInstrumentor._original_getattribute is None:
            return

        Task.__getattribute__ = TaskflowInstrumentor._original_getattribute
        TaskflowInstrumentor._original_getattribute = None


__all__ = ["TaskflowInstrumentor", "__version__"]
