from unittest import mock

import pytest
import requests
from keystoneauth1 import adapter
from keystoneauth1 import session as ks_session
from openstack import proxy

from opentelemetry.instrumentation.openstacksdk import OpenStackSDKInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode


def _make_proxy(service_type="compute", region_name="RegionOne"):
    """Build a ``Proxy`` that can run ``request`` without a live cloud.

    ``Proxy.request`` needs a connection (for its response cache) and a session
    that can report a project id; both are faked here. Caching is disabled so
    the call always reaches ``super().request`` (patched per-test).
    """
    session = mock.Mock(spec=ks_session.Session)
    session.get_project_id.return_value = None

    conn = mock.Mock()
    conn.cache_enabled = False
    conn._global_request_id = None
    conn._api_cache_keys = set()

    instance = proxy.Proxy(
        session=session,
        service_type=service_type,
        region_name=region_name,
    )
    instance._connection = conn
    return instance


def _fake_response(
    status_code=200, url="https://compute.example.com:8774/v2.1/servers"
):
    response = requests.Response()
    response.status_code = status_code
    response._content = b"{}"
    response.request = requests.Request(method="GET", url=url).prepare()
    response.history = []
    return response


@pytest.fixture(autouse=True)
def clean_instrumentation():
    OpenStackSDKInstrumentor().uninstrument()
    yield
    OpenStackSDKInstrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def instrument(span_exporter):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    instrumentor = OpenStackSDKInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)

    yield instrumentor

    instrumentor.uninstrument()


def test_instrumentation_dependencies():
    assert OpenStackSDKInstrumentor().instrumentation_dependencies() == (
        "openstacksdk",
    )


def test_request_creates_client_span(instrument, span_exporter):
    proxy_obj = _make_proxy()
    with mock.patch.object(
        adapter.Adapter, "request", return_value=_fake_response(202)
    ):
        resp = proxy_obj.request("/servers", "GET")

    assert resp.status_code == 202

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "GET servers"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == 202
    assert (
        span.attributes["url.full"]
        == "https://compute.example.com:8774/v2.1/servers"
    )
    assert span.attributes["server.address"] == "compute.example.com"
    assert span.attributes["server.port"] == 8774
    assert span.attributes["openstack.service_type"] == "compute"
    assert span.attributes["openstack.region_name"] == "RegionOne"
    assert span.status.status_code == StatusCode.UNSET


def test_trace_context_injected_into_headers(instrument, span_exporter):
    proxy_obj = _make_proxy()
    with mock.patch.object(
        adapter.Adapter, "request", return_value=_fake_response()
    ) as mocked:
        proxy_obj.request("/servers", "GET")

    # The wrapper injects a fresh headers mapping carrying the client span.
    headers = mocked.call_args.kwargs["headers"]
    assert "traceparent" in headers

    span = span_exporter.get_finished_spans()[0]
    traceparent = headers["traceparent"]
    assert format(span.context.trace_id, "032x") in traceparent
    assert format(span.context.span_id, "016x") in traceparent


def test_existing_headers_are_preserved_not_mutated(instrument):
    proxy_obj = _make_proxy()
    original = {"X-Custom": "value"}
    with mock.patch.object(
        adapter.Adapter, "request", return_value=_fake_response()
    ) as mocked:
        proxy_obj.request("/servers", "GET", headers=original)

    sent = mocked.call_args.kwargs["headers"]
    assert sent["X-Custom"] == "value"
    assert "traceparent" in sent
    # The caller's dict is copied, never mutated in place.
    assert "traceparent" not in original


def test_error_status_sets_span_error(instrument, span_exporter):
    proxy_obj = _make_proxy()
    with mock.patch.object(
        adapter.Adapter, "request", return_value=_fake_response(500)
    ):
        proxy_obj.request("/servers", "GET")

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["http.response.status_code"] == 500
    assert span.status.status_code == StatusCode.ERROR


def test_transport_exception_records_and_reraises(instrument, span_exporter):
    proxy_obj = _make_proxy()
    boom = ConnectionError("connection refused")
    with mock.patch.object(adapter.Adapter, "request", side_effect=boom):
        with pytest.raises(ConnectionError, match="connection refused"):
            proxy_obj.request("/servers", "GET")

    span = span_exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_uninstrument_stops_tracing(instrument, span_exporter):
    instrument.uninstrument()

    proxy_obj = _make_proxy()
    with mock.patch.object(
        adapter.Adapter, "request", return_value=_fake_response()
    ):
        proxy_obj.request("/servers", "GET")

    assert span_exporter.get_finished_spans() == ()
