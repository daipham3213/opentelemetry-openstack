import pytest
from taskflow import engines, retry, task
from taskflow.patterns import linear_flow

from opentelemetry.instrumentation.taskflow import TaskflowInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode


class DemoTask(task.Task):
    def execute(self, **kwargs):
        return "ok"

    def revert(self, **kwargs):
        return "reverted"


class FailingTask(task.Task):
    def execute(self, **kwargs):
        raise ValueError("boom")


@pytest.fixture(autouse=True)
def clean_instrumentation():
    TaskflowInstrumentor().uninstrument()
    yield
    TaskflowInstrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def instrument(span_exporter):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    instrumentor = TaskflowInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)

    yield instrumentor

    instrumentor.uninstrument()


def spans_by_name(span_exporter):
    """Index finished spans by span name."""
    spans = {}
    for span in span_exporter.get_finished_spans():
        spans.setdefault(span.name, []).append(span)
    return spans


def test_instrumentation_dependencies():
    assert TaskflowInstrumentor().instrumentation_dependencies() == (
        "taskflow",
    )


def test_flow_run_creates_flow_and_task_spans(instrument, span_exporter):
    flow = linear_flow.Flow("demo-flow").add(
        DemoTask(name="t1"), DemoTask(name="t2")
    )

    engines.run(flow, engine="serial")

    spans = spans_by_name(span_exporter)
    assert "taskflow.flow.run" in spans
    assert len(spans["taskflow.task.execute"]) == 2

    flow_span = spans["taskflow.flow.run"][0]
    assert flow_span.attributes["taskflow.flow.name"] == "demo-flow"
    assert "taskflow.flow.uuid" in flow_span.attributes

    names = set()
    for execute_span in spans["taskflow.task.execute"]:
        # every task span is parented to the single flow span
        assert execute_span.parent.span_id == flow_span.context.span_id
        assert execute_span.context.trace_id == flow_span.context.trace_id
        assert execute_span.attributes["taskflow.task.method"] == "execute"
        names.add(execute_span.attributes["taskflow.task.name"])
    assert names == {"t1", "t2"}


def test_failing_task_records_exception_and_reverts(instrument, span_exporter):
    flow = linear_flow.Flow("failing-flow").add(
        DemoTask(name="ok-task"), FailingTask(name="bad-task")
    )

    with pytest.raises(ValueError, match="boom"):
        engines.run(flow, engine="serial")

    spans = spans_by_name(span_exporter)

    # The failing execute span carries the exception and an error status.
    failing = next(
        s
        for s in spans["taskflow.task.execute"]
        if s.attributes["taskflow.task.name"] == "bad-task"
    )
    assert failing.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in failing.events)

    # The successful task is reverted when the flow rolls back.
    assert "taskflow.task.revert" in spans
    reverted = {
        s.attributes["taskflow.task.name"]
        for s in spans["taskflow.task.revert"]
    }
    assert "ok-task" in reverted
    assert all(
        s.attributes["taskflow.task.method"] == "revert"
        for s in spans["taskflow.task.revert"]
    )

    # The flow span itself is marked as errored.
    flow_span = spans["taskflow.flow.run"][0]
    assert flow_span.status.status_code == StatusCode.ERROR


def test_retry_controller_is_traced(instrument, span_exporter):
    flow = linear_flow.Flow(
        "retry-flow", retry=retry.Times(2, name="retry-1")
    ).add(DemoTask(name="t1"))

    engines.run(flow, engine="serial")

    spans = spans_by_name(span_exporter)
    assert "taskflow.retry.execute" in spans
    retry_span = spans["taskflow.retry.execute"][0]
    assert retry_span.attributes["taskflow.retry.name"] == "retry-1"
    assert retry_span.attributes["taskflow.retry.method"] == "execute"

    flow_span = spans["taskflow.flow.run"][0]
    assert retry_span.parent.span_id == flow_span.context.span_id


def test_parallel_engine_preserves_parentage(instrument, span_exporter):
    # The listener parents atom spans by stored context, not by the ambient
    # context of the worker thread -- so parallel execution still nests.
    flow = linear_flow.Flow("parallel-flow").add(
        DemoTask(name="t1"), DemoTask(name="t2")
    )

    engines.run(flow, engine="parallel", max_workers=2)

    spans = spans_by_name(span_exporter)
    flow_span = spans["taskflow.flow.run"][0]
    assert len(spans["taskflow.task.execute"]) == 2
    for execute_span in spans["taskflow.task.execute"]:
        assert execute_span.parent.span_id == flow_span.context.span_id


def test_uninstrument_stops_tracing(instrument, span_exporter):
    instrument.uninstrument()

    flow = linear_flow.Flow("demo-flow").add(DemoTask(name="t1"))
    engines.run(flow, engine="serial")

    assert span_exporter.get_finished_spans() == ()
