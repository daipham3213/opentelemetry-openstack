"""Tests for the producer side: trace-context injection at the transport.

``Transport._send`` / ``Transport._send_notification`` receive the
already-serialized context dictionary that goes on the wire, so the
instrumentation injects W3C trace context into it there. These tests stub the
underlying send so no real broker is required.
"""

from types import SimpleNamespace

import oslo_messaging
import pytest
from oslo_config import cfg
from oslo_messaging.transport import Transport

from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind


def _producer_span(span_exporter):
    """The single PRODUCER span the instrumentor opened for a send."""
    producers = [
        span
        for span in span_exporter.get_finished_spans()
        if span.kind == SpanKind.PRODUCER
    ]
    assert len(producers) == 1
    return producers[0]


@pytest.fixture(autouse=True)
def uninstrument_oslo_messaging():
    # The instrumentor is a singleton; guarantee a clean slate around each test.
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
def transport(monkeypatch):
    """A ``Transport`` whose send methods record the context they receive.

    The stubs are installed *before* instrumentation runs, so the instrumentor
    wraps them — exactly as it would wrap the real driver-backed methods.
    """
    sent = {}

    def _send(self, target, ctxt, message, **kwargs):
        sent["ctxt"] = ctxt
        sent["message"] = message
        return "result"

    def _send_notification(self, target, ctxt, message, version, retry=None):
        sent["ctxt"] = ctxt
        sent["message"] = message
        return "notified"

    monkeypatch.setattr(Transport, "_send", _send)
    monkeypatch.setattr(Transport, "_send_notification", _send_notification)

    transport = object.__new__(Transport)
    return transport, sent


@pytest.fixture
def instrumentor(tracer_provider):
    instrumentor = OsloMessagingInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield instrumentor
    instrumentor.uninstrument()


def test_instrumentation_dependencies():
    assert OsloMessagingInstrumentor().instrumentation_dependencies() == (
        "oslo.messaging",
    )


def test_send_injects_active_context(
    transport, instrumentor, tracer, span_exporter
):
    transport, sent = transport
    target = SimpleNamespace(topic="topic")
    ctxt = {}

    with tracer.start_as_current_span("producer") as span:
        result = transport._send(target, ctxt, {"method": "do_thing"})
        expected_trace_id = span.get_span_context().trace_id

    assert result == "result"
    # The wire context now carries the W3C trace context.
    assert "traceparent" in sent["ctxt"]
    assert format(expected_trace_id, "032x") in sent["ctxt"]["traceparent"]

    # The producer span is named after the RPC method.
    producer = _producer_span(span_exporter)
    assert producer.name == "do_thing send"
    assert producer.attributes["rpc.method"] == "do_thing"


def test_send_names_span_with_namespace(
    transport, instrumentor, tracer, span_exporter
):
    transport, sent = transport
    target = SimpleNamespace(topic="topic")

    with tracer.start_as_current_span("producer"):
        transport._send(
            target, {}, {"method": "do_thing", "namespace": "baremetal"}
        )

    assert _producer_span(span_exporter).name == "baremetal.do_thing send"


def test_send_notification_names_span_by_event_type(
    transport, instrumentor, tracer, span_exporter
):
    transport, sent = transport
    target = SimpleNamespace(topic="topic")

    with tracer.start_as_current_span("producer"):
        transport._send_notification(
            target,
            {},
            {"event_type": "compute.instance.create.start"},
            "2.0",
        )

    assert "traceparent" in sent["ctxt"]
    # A notification carries no RPC method, so the span is named by event type
    # (rather than the old "None send").
    producer = _producer_span(span_exporter)
    assert producer.name == "compute.instance.create.start send"
    assert (
        producer.attributes["oslo_messaging.notification.event_type"]
        == "compute.instance.create.start"
    )


def test_send_falls_back_to_plain_send_name(
    transport, instrumentor, tracer, span_exporter
):
    transport, sent = transport
    target = SimpleNamespace(topic="topic")

    with tracer.start_as_current_span("producer"):
        # A message with neither a method nor an event type must never produce
        # a "None send" span name.
        transport._send(target, {}, {})

    assert _producer_span(span_exporter).name == "send"


def test_non_dict_context_is_left_untouched(transport, instrumentor, tracer):
    transport, sent = transport
    target = SimpleNamespace(topic="topic")
    ctxt = SimpleNamespace()  # not a dict: nothing to inject into

    with tracer.start_as_current_span("producer"):
        transport._send(target, ctxt, {"method": "do_thing"})

    assert sent["ctxt"] is ctxt
    assert not hasattr(ctxt, "traceparent")


def test_uninstrument_restores_send(transport, instrumentor, tracer):
    transport, sent = transport
    instrumentor.uninstrument()
    target = SimpleNamespace(topic="topic")
    ctxt = {}

    with tracer.start_as_current_span("producer"):
        transport._send(target, ctxt, {"method": "do_thing"})

    assert "traceparent" not in sent["ctxt"]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("rabbit://host:5672//", "rabbitmq"),
        ("kombu://host:5672//", "rabbitmq"),  # entry-point alias for rabbit
        ("rabbit+ssl://host:5671//", "rabbitmq"),  # compound scheme
        ("fake:/", "fake"),  # no conventional value; names itself
    ],
)
def test_send_records_configured_broker_as_messaging_system(
    transport, instrumentor, span_exporter, url, expected
):
    # messaging.system is the broker, read from the transport this send is
    # going out on -- so a service whose notifications use a different
    # transport_url than its RPC reports each correctly.
    _, _sent = transport
    configured = oslo_messaging.get_rpc_transport(cfg.ConfigOpts(), url=url)

    configured._send(
        SimpleNamespace(topic="topic"), {}, {"method": "do_thing"}
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.system"] == expected
    # rpc.system is the RPC system, which is oslo.messaging regardless.
    assert span.attributes["rpc.system"] == "oslo.messaging"


def test_send_notification_records_configured_broker(
    transport, instrumentor, span_exporter
):
    _, _sent = transport
    configured = oslo_messaging.get_notification_transport(
        cfg.ConfigOpts(), url="rabbit://host:5672//"
    )

    configured._send_notification(
        SimpleNamespace(topic="topic"), {}, {"event_type": "some.event"}, "2.0"
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.system"] == "rabbitmq"


def test_messaging_system_falls_back_when_driver_is_unknown(
    transport, instrumentor, span_exporter
):
    # A transport with no resolvable driver must still carry the attribute.
    driverless, _sent = transport

    driverless._send(
        SimpleNamespace(topic="topic"), {}, {"method": "do_thing"}
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.system"] == "oslo.messaging"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"wait_for_reply": True}, "call"),
        ({}, "cast"),
        ({"wait_for_reply": None}, "cast"),
    ],
)
def test_send_records_call_style_as_messaging_operation(
    transport, instrumentor, span_exporter, kwargs, expected
):
    # wait_for_reply is what distinguishes an RPC call from a cast, and the
    # call style is what oslo.messaging's own tracing puts on the span.
    sender, _sent = transport

    sender._send(
        SimpleNamespace(topic="topic"), {}, {"method": "do_thing"}, **kwargs
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.operation"] == expected
    assert span.attributes["messaging.operation.name"] == expected


def test_send_notification_records_send_as_messaging_operation(
    transport, instrumentor, span_exporter
):
    # A notification is neither a call nor a cast.
    sender, _sent = transport

    sender._send_notification(
        SimpleNamespace(topic="topic"), {}, {"event_type": "some.event"}, "2.0"
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.operation"] == "send"


def test_send_records_topic_as_destination_and_exchange_separately(
    transport, instrumentor, span_exporter
):
    # The topic is where the message routes -- and what oslo.messaging's own
    # tracing reports. The exchange scopes it and has no conventional name.
    sender, _sent = transport
    target = SimpleNamespace(topic="compute", exchange="openstack")

    sender._send(target, {}, {"method": "do_thing"})

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["messaging.destination.name"] == "compute"
    assert span.attributes["oslo_messaging.target.exchange"] == "openstack"
    # destination.template is a low-cardinality *string* in the conventions,
    # so it must not be set to a boolean.
    assert "messaging.destination.template" not in span.attributes


def test_send_splits_namespace_into_rpc_service(
    transport, instrumentor, span_exporter
):
    sender, _sent = transport

    sender._send(
        SimpleNamespace(topic="topic"),
        {},
        {"method": "do_thing", "namespace": "baremetal"},
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.name == "baremetal.do_thing send"
    assert span.attributes["rpc.method"] == "do_thing"
    assert span.attributes["rpc.service"] == "baremetal"


def test_send_records_openstack_request_id(
    transport, instrumentor, span_exporter
):
    sender, _sent = transport

    sender._send(
        SimpleNamespace(topic="topic"),
        {"request_id": "req-abc"},
        {"method": "do_thing"},
    )

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["openstack.request_id"] == "req-abc"
    assert span.attributes["messaging.message.conversation_id"] == "req-abc"
