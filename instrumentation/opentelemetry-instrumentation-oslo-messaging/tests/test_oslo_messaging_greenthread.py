"""Greenthread propagation: work spawned via eventlet keeps the active trace.

eventlet gives each greenthread its own ``contextvars`` context, so a callable
handed to ``spawn``/``spawn_n``/``spawn_after``/``GreenPool.spawn`` starts with an
empty context and opens a disconnected trace -- this is what breaks the trace at
nova-compute's ``build_and_run_instance``, which spawns the real work right after
the RPC is dispatched. The instrumentor wraps the eventlet spawns to carry the
context across; these tests exercise that.

No ``eventlet.monkey_patch()`` is needed: greenlet isolates ``contextvars`` per
greenthread on its own, so the break (and the fix) reproduce without patching the
stdlib -- which keeps the test run clean.
"""

import eventlet
import pytest
from eventlet.greenpool import GreenPool

from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind


@pytest.fixture(autouse=True)
def uninstrument_oslo_messaging():
    OsloMessagingInstrumentor().uninstrument()
    yield
    OsloMessagingInstrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(span_exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return provider


@pytest.fixture
def tracer(tracer_provider):
    return tracer_provider.get_tracer(__name__)


@pytest.fixture
def instrumentor(tracer_provider):
    inst = OsloMessagingInstrumentor()
    inst.instrument(tracer_provider=tracer_provider)
    yield inst
    inst.uninstrument()


def _spawned_work(tracer):
    """A callable that opens a span and reports its trace id and parent id."""

    def work():
        with tracer.start_as_current_span("greenthread.work") as span:
            ctx = span.get_span_context()
            return ctx.trace_id, span.parent.span_id if span.parent else None

    return work


def test_spawn_keeps_work_on_consumer_trace(instrumentor, tracer):
    # Model the nova case: a consumer span, then work handed to a greenthread.
    work = _spawned_work(tracer)
    with tracer.start_as_current_span(
        "build_and_run_instance receive", kind=SpanKind.CONSUMER
    ) as consumer:
        consumer_ctx = consumer.get_span_context()
        greenthread = eventlet.spawn(work)

    trace_id, parent_span_id = greenthread.wait()
    assert trace_id == consumer_ctx.trace_id
    assert parent_span_id == consumer_ctx.span_id


def test_spawn_n_propagates_context(instrumentor, tracer):
    captured = {}

    def work():
        with tracer.start_as_current_span("greenthread.work") as span:
            captured["ctx"] = span.get_span_context()
            captured["parent"] = span.parent

    with tracer.start_as_current_span("parent") as parent:
        parent_ctx = parent.get_span_context()
        eventlet.spawn_n(work)
        eventlet.sleep(0)  # yield so the greenthread runs

    assert captured["ctx"].trace_id == parent_ctx.trace_id
    assert captured["parent"].span_id == parent_ctx.span_id


def test_spawn_after_propagates_context(instrumentor, tracer):
    # spawn_after(seconds, func, ...): the callable is the *second* argument.
    work = _spawned_work(tracer)
    with tracer.start_as_current_span("parent") as parent:
        parent_ctx = parent.get_span_context()
        greenthread = eventlet.spawn_after(0, work)

    trace_id, parent_span_id = greenthread.wait()
    assert trace_id == parent_ctx.trace_id
    assert parent_span_id == parent_ctx.span_id


def test_greenpool_spawn_propagates_context(instrumentor, tracer):
    work = _spawned_work(tracer)
    pool = GreenPool()
    with tracer.start_as_current_span("parent") as parent:
        parent_ctx = parent.get_span_context()
        greenthread = pool.spawn(work)

    trace_id, parent_span_id = greenthread.wait()
    assert trace_id == parent_ctx.trace_id
    assert parent_span_id == parent_ctx.span_id


def test_uninstrument_stops_propagation(instrumentor, tracer):
    instrumentor.uninstrument()

    work = _spawned_work(tracer)
    with tracer.start_as_current_span("parent") as parent:
        parent_ctx = parent.get_span_context()
        greenthread = eventlet.spawn(work)

    trace_id, _ = greenthread.wait()
    # Back to the broken behaviour: the greenthread runs on a fresh context, so
    # its span lands on a different trace.
    assert trace_id != parent_ctx.trace_id
