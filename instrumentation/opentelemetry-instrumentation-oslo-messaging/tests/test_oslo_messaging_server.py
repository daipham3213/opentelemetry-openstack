"""Tests for Strategy 2: server-side (consumer) instrumentation.

These exercise ``RPCDispatcher.dispatch`` and
``NotificationDispatcher.dispatch`` producing ``CONSUMER`` spans, and verify
that a span parents to the producer via the trace context carried on the wire.
"""

from types import SimpleNamespace

import pytest
from oslo_messaging._drivers import amqpdriver, impl_fake
from oslo_messaging.notify.dispatcher import NotificationDispatcher
from oslo_messaging.rpc.dispatcher import RPCDispatcher
from oslo_messaging.serializer import NoOpSerializer

from opentelemetry import context as context_api
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
    decorators,
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
    assert span.name == "echo receive"
    assert span.kind == SpanKind.CONSUMER
    # SimpleNamespace is not a driver message, so this is the fallback; the
    # broker-derived values are covered below.
    assert span.attributes["messaging.system"] == "oslo.messaging"
    assert span.attributes["messaging.operation"] == "receive"
    assert span.attributes["messaging.operation.name"] == "receive"
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
    assert span.name == "event.type receive"
    assert span.kind == SpanKind.CONSUMER
    assert span.attributes["messaging.operation"] == "receive"
    assert span.attributes["messaging.operation.name"] == "receive"
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
        if s.name == "echo receive"
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
    assert spans[0].name == "boom receive"
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in spans[0].events)


def test_uninstrument_restores_dispatch(instrumentor, span_exporter):
    instrumentor.uninstrument()
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    dispatcher.dispatch(_rpc_incoming("echo"))

    assert span_exporter.get_finished_spans() == ()


def _traceparent(trace_id="1" * 32, span_id="2" * 16):
    return {"traceparent": f"00-{trace_id}-{span_id}-01"}


def test_rpc_dispatch_does_not_leak_context_when_endpoint_raises(
    instrumentor, span_exporter
):
    # Executors hand messages to a pool of reused threads/greenthreads, so a
    # context left attached by a raising handler would pin that worker to a
    # finished message's trace -- and a later message arriving without a
    # traceparent would be adopted into it. Endpoints raising is routine.
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    before = trace.get_current_span().get_span_context()

    with pytest.raises(RuntimeError, match="boom"):
        dispatcher.dispatch(_rpc_incoming("boom", ctxt=_traceparent()))

    after = trace.get_current_span().get_span_context()
    assert after.trace_id == before.trace_id
    assert after.span_id == before.span_id


@pytest.mark.parametrize(
    "build_wrapper",
    [decorators.rpc_server_wrapper, decorators.notification_server_wrapper],
)
def test_consumer_wrapper_detaches_context_when_dispatch_raises(
    tracer, build_wrapper
):
    # The notification dispatcher swallows callback errors, so drive the
    # wrapper contract directly: whatever it wraps, a raised exception must
    # still leave the context as it was found.
    wrapper = build_wrapper(tracer)
    incoming = SimpleNamespace(
        ctxt=_traceparent(), message={"method": "m", "event_type": "e"}
    )

    def raising(*args, **kwargs):
        raise RuntimeError("boom")

    before = trace.get_current_span().get_span_context()

    with pytest.raises(RuntimeError, match="boom"):
        wrapper(raising, None, (incoming,), {})

    after = trace.get_current_span().get_span_context()
    assert after.trace_id == before.trace_id
    assert after.span_id == before.span_id


def test_next_message_without_traceparent_is_not_adopted_into_previous_trace(
    instrumentor, span_exporter
):
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    with pytest.raises(RuntimeError, match="boom"):
        dispatcher.dispatch(_rpc_incoming("boom", ctxt=_traceparent()))

    # Same worker, next message, no wire context: it must start its own trace.
    dispatcher.dispatch(_rpc_incoming("echo"))

    failed, ok = (
        s
        for s in sorted(
            span_exporter.get_finished_spans(), key=lambda s: s.start_time
        )
    )
    assert f"{failed.context.trace_id:032x}" == "1" * 32
    assert ok.context.trace_id != failed.context.trace_id
    assert ok.parent is None


class _ForkedRabbitMessage(amqpdriver.AMQPIncomingMessage):
    """Stands in for an out-of-tree driver subclassing an in-tree message."""

    def __init__(self):
        pass


@pytest.mark.parametrize(
    "message_class,expected",
    [
        (amqpdriver.AMQPIncomingMessage, "rabbitmq"),
        (amqpdriver.NotificationAMQPIncomingMessage, "rabbitmq"),
        (_ForkedRabbitMessage, "rabbitmq"),  # resolved through the MRO
        (impl_fake.FakeIncomingMessage, "fake"),
        (SimpleNamespace, "oslo.messaging"),  # not a driver message
    ],
)
def test_consumer_span_records_broker_the_message_arrived_from(
    tracer, span_exporter, message_class, expected
):
    # Dispatchers never see the Transport, so the driver-specific class of the
    # message they are handed is what identifies the broker.
    wrapper = decorators.rpc_server_wrapper(tracer)
    incoming = message_class.__new__(message_class)
    incoming.ctxt = {}
    incoming.message = {"method": "echo"}

    wrapper(lambda *args, **kwargs: None, None, (incoming,), {})

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.system"] == expected
    assert span.attributes["rpc.system"] == "oslo.messaging"


def test_notification_consumer_span_records_broker(tracer, span_exporter):
    wrapper = decorators.notification_server_wrapper(tracer)
    incoming = object.__new__(amqpdriver.NotificationAMQPIncomingMessage)
    incoming.ctxt = {}
    incoming.message = {"event_type": "some.event", "priority": "info"}

    wrapper(lambda *args, **kwargs: None, None, (incoming,), {})

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.system"] == "rabbitmq"


def test_rpc_consumer_splits_namespace_into_rpc_service(
    instrumentor, tracer, span_exporter
):
    # rpc.method is the bare method and the namespace goes to rpc.service --
    # what the conventions describe and what oslo.messaging's own tracing
    # records. The dotted form still names the span.
    incoming = _rpc_incoming("echo")
    incoming.message["namespace"] = "baremetal"

    decorators.rpc_server_wrapper(tracer)(
        lambda *args, **kwargs: None, None, (incoming,), {}
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.name == "baremetal.echo receive"
    assert span.attributes["rpc.method"] == "echo"
    assert span.attributes["rpc.service"] == "baremetal"


def test_rpc_consumer_omits_rpc_service_without_a_namespace(
    instrumentor, span_exporter
):
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    dispatcher.dispatch(_rpc_incoming("echo"))

    span = span_exporter.get_finished_spans()[0]
    assert span.name == "echo receive"
    assert "rpc.service" not in span.attributes


@pytest.mark.parametrize(
    "build_incoming",
    [_rpc_incoming, lambda *a, **kw: _notification_incoming(**kw)],
)
def test_consumer_records_openstack_request_id(
    instrumentor, tracer, span_exporter, build_incoming
):
    # oslo.messaging's own tracing records the request id under
    # openstack.request_id; conversation_id is the portable equivalent.
    incoming = build_incoming("echo", ctxt={"request_id": "req-abc"})
    wrapper = (
        decorators.rpc_server_wrapper(tracer)
        if "method" in incoming.message
        else decorators.notification_server_wrapper(tracer)
    )

    wrapper(lambda *args, **kwargs: None, None, (incoming,), {})

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["openstack.request_id"] == "req-abc"
    assert span.attributes["messaging.message.conversation_id"] == "req-abc"


def test_consumer_omits_request_id_when_absent(instrumentor, span_exporter):
    dispatcher = RPCDispatcher([_RpcEndpoint()], NoOpSerializer())

    dispatcher.dispatch(_rpc_incoming("echo"))

    span = span_exporter.get_finished_spans()[0]
    assert "openstack.request_id" not in span.attributes
