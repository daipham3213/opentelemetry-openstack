import pytest
from taskflow import task

from opentelemetry.instrumentation.taskflow import TaskflowInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode


class DemoTask(task.Task):
    def execute(self, **kwargs):
        return kwargs.get("value", "ok")

    def revert(self, *args, **kwargs):
        return kwargs.get("result", "reverted")


class FailingTask(task.Task):
    def execute(self, **kwargs):
        raise ValueError(kwargs.get("message", "boom"))


@pytest.fixture(autouse=True)
def uninstrument_taskflow():
    TaskflowInstrumentor().uninstrument()
    yield
    TaskflowInstrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def instrumentor(span_exporter):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    instrumentor = TaskflowInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)

    yield instrumentor

    instrumentor.uninstrument()


def assert_task_span(span, *, method, task_name, task_class="DemoTask"):
    assert span.name == f"taskflow.task.{method}"
    assert span.attributes["taskflow.task.class"].endswith(f".{task_class}")
    assert span.attributes["taskflow.task.method"] == method
    assert span.attributes["taskflow.task.name"] == task_name


def test_instrumentation_dependencies():
    assert TaskflowInstrumentor().instrumentation_dependencies() == (
        "taskflow",
    )


@pytest.mark.parametrize(
    ("method", "kwargs", "expected"),
    (
        ("execute", {"value": "done"}, "done"),
        ("revert", {"result": "rolled-back"}, "rolled-back"),
    ),
)
def test_task_lifecycle_methods_create_spans(
    instrumentor,
    span_exporter,
    method,
    kwargs,
    expected,
):
    result = getattr(DemoTask(name="demo-task"), method)(**kwargs)

    spans = span_exporter.get_finished_spans()
    assert result == expected
    assert len(spans) == 1
    assert_task_span(spans[0], method=method, task_name="demo-task")


def test_subclass_overrides_are_wrapped(instrumentor, span_exporter):
    # ``execute`` is defined on the subclass, not on ``Task`` — wrapping
    # ``__getattribute__`` is what lets the instrumentor see it.
    assert DemoTask(name="subclass-task").execute(value="done") == "done"

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert_task_span(spans[0], method="execute", task_name="subclass-task")


def test_instrument_twice_does_not_create_nested_duplicate_spans(
    instrumentor,
    span_exporter,
):
    instrumentor.instrument()

    assert DemoTask(name="demo-task").execute(value="done") == "done"

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert_task_span(spans[0], method="execute", task_name="demo-task")


def test_uninstrument_restores_task_without_creating_spans(
    instrumentor,
    span_exporter,
):
    instrumentor.uninstrument()

    result = DemoTask(name="demo-task").execute(value="done")

    assert result == "done"
    assert span_exporter.get_finished_spans() == ()


def test_exception_is_recorded_on_span(instrumentor, span_exporter):
    with pytest.raises(ValueError, match="boom"):
        FailingTask(name="failing-task").execute(message="boom")

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert_task_span(
        spans[0],
        method="execute",
        task_name="failing-task",
        task_class="FailingTask",
    )
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in spans[0].events)
