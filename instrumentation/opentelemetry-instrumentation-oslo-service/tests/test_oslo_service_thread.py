"""Native-thread and futurist-pool propagation: spawned work keeps the trace.

Python does not copy the ``contextvars`` context into a new thread, so a span
opened on a worker thread would land on a fresh trace. ``OsloServiceInstrumentor``
wraps ``threading.Thread`` (used by oslo_service's threading-backend
``ThreadGroup.add_thread``) and ``futurist`` pools (used by its looping/periodic
calls) to carry the context across. These tests exercise that via span parenting.
"""

import threading

import pytest

from opentelemetry.instrumentation.oslo_service import OsloServiceInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture(autouse=True)
def uninstrument_oslo_service():
    OsloServiceInstrumentor().uninstrument()
    yield
    OsloServiceInstrumentor().uninstrument()


@pytest.fixture
def tracer_provider():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    return provider


@pytest.fixture
def tracer(tracer_provider):
    return tracer_provider.get_tracer(__name__)


@pytest.fixture
def instrumentor():
    inst = OsloServiceInstrumentor()
    inst.instrument()
    yield inst
    inst.uninstrument()


def _run_in_thread(target):
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()


def test_thread_keeps_work_on_parent_trace(instrumentor, tracer):
    captured = {}

    def work():
        with tracer.start_as_current_span("thread.work") as span:
            captured["ctx"] = span.get_span_context()
            captured["parent"] = span.parent

    # oslo_service's threading backend ThreadGroup.add_thread spawns a bare
    # threading.Thread per task; a raw Thread exercises the same path.
    with tracer.start_as_current_span("parent") as parent:
        parent_ctx = parent.get_span_context()
        _run_in_thread(work)

    assert captured["ctx"].trace_id == parent_ctx.trace_id
    assert captured["parent"].span_id == parent_ctx.span_id


def test_futurist_pool_keeps_work_on_parent_trace(instrumentor, tracer):
    futurist = pytest.importorskip("futurist")
    captured = {}

    def work():
        with tracer.start_as_current_span("pool.work") as span:
            captured["ctx"] = span.get_span_context()
            captured["parent"] = span.parent

    executor = futurist.ThreadPoolExecutor(max_workers=1)
    try:
        with tracer.start_as_current_span("parent") as parent:
            parent_ctx = parent.get_span_context()
            executor.submit(work).result()
    finally:
        executor.shutdown()

    assert captured["ctx"].trace_id == parent_ctx.trace_id
    assert captured["parent"].span_id == parent_ctx.span_id


def test_uninstrument_stops_thread_propagation(instrumentor, tracer):
    instrumentor.uninstrument()
    captured = {}

    def work():
        with tracer.start_as_current_span("thread.work") as span:
            captured["ctx"] = span.get_span_context()

    with tracer.start_as_current_span("parent") as parent:
        parent_ctx = parent.get_span_context()
        _run_in_thread(work)

    # Back to the broken behaviour: the worker thread runs on a fresh context.
    assert captured["ctx"].trace_id != parent_ctx.trace_id


def test_instrument_wraps_threading_thread_and_uninstrument_restores():
    original_start = threading.Thread.start
    instrumentor = OsloServiceInstrumentor()

    instrumentor.instrument()
    assert threading.Thread.start is not original_start

    instrumentor.uninstrument()
    assert threading.Thread.start is original_start


def test_instrument_is_safe_without_active_span(instrumentor, tracer):
    # No span active: spawned work simply runs without correlation, no error.
    result = {}

    def work():
        result["ran"] = True

    _run_in_thread(work)
    assert result["ran"] is True
