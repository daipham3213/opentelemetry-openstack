from types import SimpleNamespace

import pytest
from oslo_messaging.notify.notifier import Notifier
from oslo_messaging.rpc.client import RPCClient

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
def uninstrument_oslo_messaging():
    OsloMessagingInstrumentor().uninstrument()
    yield
    OsloMessagingInstrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def instrumentor(span_exporter):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    instrumentor = OsloMessagingInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)

    yield instrumentor

    instrumentor.uninstrument()


def _echo(self, ctxt, method, **kwargs):
    """Stand-in RPC method returning its arguments so they can be asserted."""
    return {"context": ctxt, "method": method, "kwargs": kwargs}


@pytest.fixture
def rpc_client(monkeypatch):
    # Patch the methods *before* instrument() runs (the instrumentor wraps
    # whatever it finds), so the wrapper delegates to ``_echo``.
    monkeypatch.setattr(RPCClient, "call", _echo)
    monkeypatch.setattr(RPCClient, "cast", _echo)

    client = object.__new__(RPCClient)
    client.target = SimpleNamespace(
        exchange="exchange",
        topic="topic",
        server="server",
        namespace="namespace",
        version="1.0",
    )
    return client


@pytest.fixture
def raising_rpc_client(monkeypatch):
    def call(self, ctxt, method, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(RPCClient, "call", call)

    client = object.__new__(RPCClient)
    client.target = SimpleNamespace(topic="topic")
    return client


@pytest.fixture
def notifier(monkeypatch):
    def notify(self, ctxt, event_type, payload, priority):
        return {
            "context": ctxt,
            "event_type": event_type,
            "payload": payload,
            "priority": priority,
        }

    monkeypatch.setattr(Notifier, "_notify", notify)

    notifier = object.__new__(Notifier)
    notifier.publisher_id = "publisher"
    return notifier


def assert_rpc_span(span, *, operation, span_kind):
    assert span.name == f"oslo.messaging.rpc.{operation}"
    assert span.kind == span_kind
    assert span.attributes["messaging.system"] == "oslo.messaging"
    assert span.attributes["messaging.operation.name"] == operation
    assert span.attributes["rpc.system"] == "oslo.messaging"
    assert span.attributes["rpc.method"] == "method"
    assert span.attributes["oslo_messaging.exchange"] == "exchange"
    assert span.attributes["oslo_messaging.topic"] == "topic"
    assert span.attributes["oslo_messaging.server"] == "server"
    assert span.attributes["oslo_messaging.namespace"] == "namespace"
    assert span.attributes["oslo_messaging.version"] == "1.0"


def assert_notification_span(span, *, event_type, priority, publisher_id):
    assert span.name == "oslo.messaging.notification.publish"
    assert span.kind == SpanKind.PRODUCER
    assert span.attributes["messaging.system"] == "oslo.messaging"
    assert span.attributes["messaging.operation.name"] == "publish"
    assert span.attributes["oslo_messaging.notification.priority"] == priority
    assert (
        span.attributes["oslo_messaging.notification.event_type"] == event_type
    )
    assert (
        span.attributes["oslo_messaging.notification.publisher_id"]
        == publisher_id
    )


def test_instrumentation_dependencies():
    assert OsloMessagingInstrumentor().instrumentation_dependencies() == (
        "oslo.messaging",
    )


@pytest.mark.parametrize(
    ("operation", "span_kind"),
    (
        ("call", SpanKind.CLIENT),
        ("cast", SpanKind.PRODUCER),
    ),
)
def test_rpc_client_methods_create_spans(
    rpc_client,
    instrumentor,
    span_exporter,
    operation,
    span_kind,
):
    context = {}
    result = getattr(rpc_client, operation)(context, "method", value="done")

    assert result["context"] is context
    assert result["method"] == "method"
    assert result["kwargs"] == {"value": "done"}
    assert "traceparent" in context["_otel_context"]

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert_rpc_span(spans[0], operation=operation, span_kind=span_kind)


def test_notification_notify_creates_span(
    notifier,
    instrumentor,
    span_exporter,
):
    context = {}
    payload = {"secret": "not captured"}
    result = notifier._notify(context, "event.type", payload, "info")

    assert result["context"] is context
    assert result["event_type"] == "event.type"
    assert result["payload"] is payload
    assert result["priority"] == "info"
    assert "traceparent" in context["_otel_context"]

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert_notification_span(
        spans[0],
        event_type="event.type",
        priority="info",
        publisher_id="publisher",
    )
    # The payload must never leak into span attributes.
    assert "secret" not in spans[0].attributes


def test_instrument_twice_does_not_create_nested_duplicate_spans(
    rpc_client,
    instrumentor,
    span_exporter,
):
    instrumentor.instrument()

    rpc_client.call({}, "method")

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert_rpc_span(spans[0], operation="call", span_kind=SpanKind.CLIENT)


def test_uninstrument_restores_methods_without_creating_spans(
    rpc_client,
    instrumentor,
    span_exporter,
):
    instrumentor.uninstrument()

    result = rpc_client.call({}, "method")

    assert result["method"] == "method"
    assert span_exporter.get_finished_spans() == ()


def test_rpc_exception_is_recorded(
    raising_rpc_client,
    instrumentor,
    span_exporter,
):
    with pytest.raises(RuntimeError, match="boom"):
        raising_rpc_client.call({}, "method")

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "oslo.messaging.rpc.call"
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in spans[0].events)
