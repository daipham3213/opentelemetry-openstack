"""Tests for Strategy 2: server-side (consumer) instrumentation.

These exercise ``RPCDispatcher.dispatch`` and
``NotificationDispatcher.dispatch`` producing ``CONSUMER`` spans, and verify
that a span parents to the producer via the trace context carried on the wire.
"""

from types import SimpleNamespace

import pytest
from oslo_messaging.notify.dispatcher import NotificationDispatcher
from oslo_messaging.rpc.dispatcher import RPCDispatcher
from oslo_messaging.serializer import NoOpSerializer

from opentelemetry import context as context_api
from opentelemetry import propagate
from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode


@pytest.fixture(autouse=True)
def reset_otel_context():
    token = context_api.attach(context_api.Context())
    yield
    context_api.detach(token)


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
    instrumentor = OsloMessagingInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield instrumentor
    instrumentor.uninstrument()


class _RpcEndpoint:
    """Minimal RPC endpoint exposing methods under the default namespace."""

    def echo(self, ctxt, **kwargs):
        return {"ctxt": ctxt, "kwargs": kwargs}

    def boom(self, ctxt, **kwargs):
        raise RuntimeError("boom")


class _NotificationEndpoint:
    """Minimal notification endpoint handling ``info`` priority."""

    def __init__(self):
        self.received = []

    def info(self, ctxt, publisher_id, event_type, payload, metadata):
        self.received.append((event_type, payload))


def _rpc_incoming(method, ctxt=None):
    return SimpleNamespace(
        ctxt=ctxt if ctxt is not None else {},
        message={
            "method": method,
            "args": {},
            "namespace": None,
            "version": "1.0",
        },
        client_timeout=0,
    )


def _notification_incoming(ctxt=None):
    return SimpleNamespace(
        ctxt=ctxt if ctxt is not None else {},
        message={
            "priority": "info",
            "publisher_id": "publisher",
            "event_type": "event.type",
            "payload": {"secret": "not captured"},
            "message_id": "id-1",
            "timestamp": "now",
        },
    )


def test_rpc_dispatch_creates_consumer_span(instrumentor, span_exporter):
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    result = dispatcher.dispatch(_rpc_incoming("echo"))

    assert result["kwargs"] == {}
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "oslo.messaging.rpc.process"
    assert span.kind == SpanKind.CONSUMER
    assert span.attributes["messaging.system"] == "oslo.messaging"
    assert span.attributes["messaging.operation.name"] == "process"
    assert span.attributes["rpc.system"] == "oslo.messaging"
    assert span.attributes["rpc.method"] == "echo"


def test_notification_dispatch_creates_consumer_span(
    instrumentor, span_exporter
):
    endpoint = _NotificationEndpoint()
    dispatcher = NotificationDispatcher([endpoint], NoOpSerializer())

    dispatcher.dispatch(_notification_incoming())

    assert endpoint.received == [("event.type", {"secret": "not captured"})]
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "oslo.messaging.notification.process"
    assert span.kind == SpanKind.CONSUMER
    assert span.attributes["messaging.operation.name"] == "process"
    assert (
        span.attributes["oslo_messaging.notification.event_type"]
        == "event.type"
    )
    assert span.attributes["oslo_messaging.notification.priority"] == "info"
    # The payload must never leak into span attributes.
    assert "secret" not in span.attributes


def test_consumer_span_parents_to_producer_via_wire_context(
    instrumentor, tracer, span_exporter
):
    # Producer side: inject the active context into the wire ``ctxt`` (this is
    # what ``Transport._send`` does in production).
    with tracer.start_as_current_span("producer") as producer:
        carrier = {}
        propagate.inject(carrier)
        producer_ctx = producer.get_span_context()

    # Consumer side: the dispatch span should adopt the producer as parent.
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())
    dispatcher.dispatch(_rpc_incoming("echo", ctxt=carrier))

    consumer = next(
        s
        for s in span_exporter.get_finished_spans()
        if s.name == "oslo.messaging.rpc.process"
    )
    assert consumer.parent is not None
    assert consumer.parent.trace_id == producer_ctx.trace_id
    assert consumer.parent.span_id == producer_ctx.span_id


def test_rpc_dispatch_records_endpoint_exception(instrumentor, span_exporter):
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    with pytest.raises(RuntimeError, match="boom"):
        dispatcher.dispatch(_rpc_incoming("boom"))

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "oslo.messaging.rpc.process"
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in spans[0].events)


def test_uninstrument_restores_dispatch(instrumentor, span_exporter):
    instrumentor.uninstrument()
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    dispatcher.dispatch(_rpc_incoming("echo"))

    assert span_exporter.get_finished_spans() == ()
