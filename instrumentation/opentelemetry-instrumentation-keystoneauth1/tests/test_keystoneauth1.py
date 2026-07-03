from unittest import mock

import pytest
import requests
from keystoneauth1 import session as ks_session

from opentelemetry.instrumentation.keystoneauth1 import (
    KeystoneAuth1Instrumentor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

_URL = "https://compute.example.com:8774/v2.1/servers"


def _fake_response(status_code=200, url=_URL):
    """A ``requests.Response`` that ``Session.request`` can post-process.

    ``Session.request`` reads the resolved URL back off ``response.request``
    and, on error statuses, parses a JSON error body; both are provided here so
    the call runs without a live cloud.
    """
    response = requests.Response()
    response.status_code = status_code
    response._content = b"{}"
    response.request = requests.Request(method="GET", url=url).prepare()
    response.history = []
    response.headers["Content-Type"] = "application/json"
    return response


def _request(session, url=_URL, method="GET", **kwargs):
    """Issue a request with auth disabled (no plugin needed for the test)."""
    kwargs.setdefault("authenticated", False)
    return session.request(url, method, **kwargs)


@pytest.fixture(autouse=True)
def clean_instrumentation():
    KeystoneAuth1Instrumentor().uninstrument()
    yield
    KeystoneAuth1Instrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def instrument(span_exporter):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    instrumentor = KeystoneAuth1Instrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)

    yield instrumentor

    instrumentor.uninstrument()


def test_instrumentation_dependencies():
    assert KeystoneAuth1Instrumentor().instrumentation_dependencies() == (
        "keystoneauth1",
    )


def test_request_creates_client_span(instrument, span_exporter):
    session = ks_session.Session()
    with mock.patch.object(
        ks_session.Session, "_send_request", return_value=_fake_response(202)
    ) as mock_send:
        resp = _request(
            session,
            endpoint_filter={"service_type": "compute"},
            global_request_id="req-abc",
        )
        assert mock_send.call_count == 1

    assert resp.status_code == 202

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "GET compute"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == 202
    assert span.attributes["url.full"] == _URL
    assert span.attributes["server.address"] == "compute.example.com"
    assert span.attributes["server.port"] == 8774
    assert span.attributes["openstack.service_type"] == "compute"
    assert span.attributes["messaging.message.conversation_id"] == "req-abc"
    assert span.status.status_code == StatusCode.UNSET


def test_request_without_endpoint_filter_names_by_method(
    instrument, span_exporter
):
    session = ks_session.Session()
    with mock.patch.object(
        ks_session.Session, "_send_request", return_value=_fake_response()
    ):
        _request(session)

    span = span_exporter.get_finished_spans()[0]
    # Token/discovery requests carry no endpoint_filter -> no service_type.
    assert span.name == "GET"
    assert "openstack.service_type" not in span.attributes


def test_trace_context_injected_into_headers(instrument, span_exporter):
    session = ks_session.Session()
    with mock.patch.object(
        ks_session.Session, "_send_request", return_value=_fake_response()
    ) as mock_send:
        _request(session)

    headers = mock_send.call_args.kwargs["headers"]
    assert "traceparent" in headers

    span = span_exporter.get_finished_spans()[0]
    traceparent = headers["traceparent"]
    assert format(span.context.trace_id, "032x") in traceparent
    assert format(span.context.span_id, "016x") in traceparent


def test_existing_headers_are_preserved_not_mutated(instrument):
    session = ks_session.Session()
    original = {"X-Custom": "value"}
    with mock.patch.object(
        ks_session.Session, "_send_request", return_value=_fake_response()
    ) as mock_send:
        _request(session, headers=original)

    sent = mock_send.call_args.kwargs["headers"]
    assert sent["X-Custom"] == "value"
    assert "traceparent" in sent
    # The caller's dict is copied, never mutated in place.
    assert "traceparent" not in original


def test_error_status_raises_and_sets_span_error(instrument, span_exporter):
    session = ks_session.Session()
    with mock.patch.object(
        ks_session.Session,
        "_send_request",
        return_value=_fake_response(500),
    ):
        # raise_exc defaults to True, so an error status becomes an exception.
        with pytest.raises(Exception):
            _request(session, endpoint_filter={"service_type": "compute"})

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["http.response.status_code"] == 500
    assert span.attributes["url.full"] == _URL
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_error_status_recorded_when_raise_exc_false(instrument, span_exporter):
    session = ks_session.Session()
    with mock.patch.object(
        ks_session.Session,
        "_send_request",
        return_value=_fake_response(404),
    ):
        resp = _request(session, raise_exc=False)

    assert resp.status_code == 404
    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["http.response.status_code"] == 404
    assert span.status.status_code == StatusCode.ERROR


def test_transport_exception_records_and_reraises(instrument, span_exporter):
    session = ks_session.Session()
    boom = ConnectionError("connection refused")
    with mock.patch.object(
        ks_session.Session, "_send_request", side_effect=boom
    ):
        with pytest.raises(ConnectionError, match="connection refused"):
            _request(session)

    span = span_exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_uninstrument_stops_tracing(instrument, span_exporter):
    instrument.uninstrument()

    session = ks_session.Session()
    with mock.patch.object(
        ks_session.Session, "_send_request", return_value=_fake_response()
    ):
        _request(session)

    assert span_exporter.get_finished_spans() == ()
