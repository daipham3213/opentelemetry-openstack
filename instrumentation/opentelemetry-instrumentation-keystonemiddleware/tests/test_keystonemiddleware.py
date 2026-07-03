import pytest
from keystonemiddleware.auth_token import BaseAuthProtocol
from keystonemiddleware.auth_token._request import _AuthTokenRequest

from opentelemetry.instrumentation.keystonemiddleware import (
    KeystoneMiddlewareInstrumentor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode


def _token_body(expires="2999-01-01T00:00:00.000000Z"):
    """A minimal, well-formed Keystone v3 token body.

    Built by hand (rather than via ``keystoneauth1.fixture``) so the tests do
    not pull in the extra ``fixtures`` dependency.
    """
    return {
        "token": {
            "methods": ["password"],
            "expires_at": expires,
            "user": {
                "id": "u1",
                "name": "alice",
                "domain": {"id": "default", "name": "Default"},
            },
            "project": {
                "id": "p1",
                "name": "proj",
                "domain": {"id": "default", "name": "Default"},
            },
            "roles": [
                {"id": "r1", "name": "admin"},
                {"id": "r2", "name": "member"},
            ],
            "catalog": [],
            "audit_ids": ["auditid"],
        }
    }


class _FakeAuthProtocol(BaseAuthProtocol):
    """A ``BaseAuthProtocol`` whose token fetch is stubbed for tests.

    ``fetch_token`` is what talks to Keystone (or the cache) in the real
    middleware; here it just returns canned data or raises a canned error, so
    ``process_request`` -- the method under instrumentation -- runs end to end
    without a live cloud.
    """

    def __init__(self, token_data=None, exc=None):
        super().__init__(app=None)
        self._token_data = token_data
        self._exc = exc

    def fetch_token(self, token, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._token_data


def _request(**headers):
    return _AuthTokenRequest.blank("/v3/servers", headers=headers)


@pytest.fixture(autouse=True)
def clean_instrumentation():
    KeystoneMiddlewareInstrumentor().uninstrument()
    yield
    KeystoneMiddlewareInstrumentor().uninstrument()


@pytest.fixture
def span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(span_exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return provider


@pytest.fixture
def instrument(tracer_provider):
    instrumentor = KeystoneMiddlewareInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield instrumentor
    instrumentor.uninstrument()


def test_instrumentation_dependencies():
    assert KeystoneMiddlewareInstrumentor().instrumentation_dependencies() == (
        "keystonemiddleware",
    )


def test_valid_user_token_records_identity(instrument, span_exporter):
    middleware = _FakeAuthProtocol(_token_body())
    request = _request(**{"X-Auth-Token": "TOKENID"})

    assert middleware.process_request(request) is None

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert span.name == "keystonemiddleware.authenticate"
    # No active parent span -> this middleware is the trace entry point.
    assert span.kind == SpanKind.SERVER
    assert span.attributes["keystonemiddleware.user_token.present"] is True
    assert span.attributes["keystonemiddleware.user_token.valid"] is True
    assert span.attributes["keystonemiddleware.service_token.present"] is False
    assert span.attributes["user.id"] == "u1"
    assert span.attributes["user.name"] == "alice"
    assert span.attributes["user.roles"] == ("admin", "member")
    assert span.attributes["openstack.project_id"] == "p1"
    assert span.attributes["openstack.project_name"] == "proj"
    assert span.status.status_code == StatusCode.UNSET


def test_expired_user_token_is_marked_invalid(instrument, span_exporter):
    middleware = _FakeAuthProtocol(
        _token_body(expires="2000-01-01T00:00:00.000000Z")
    )
    request = _request(**{"X-Auth-Token": "EXPIRED"})

    middleware.process_request(request)

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["keystonemiddleware.user_token.present"] is True
    assert span.attributes["keystonemiddleware.user_token.valid"] is False
    # A rejected token is a normal auth outcome, not a span error, and no
    # identity is recorded for it.
    assert span.status.status_code == StatusCode.UNSET
    assert "user.id" not in span.attributes


def test_missing_user_token(instrument, span_exporter):
    middleware = _FakeAuthProtocol(_token_body())
    request = _request()

    middleware.process_request(request)

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["keystonemiddleware.user_token.present"] is False
    assert span.attributes["keystonemiddleware.service_token.present"] is False
    assert "keystonemiddleware.user_token.valid" not in span.attributes


def test_service_token_recorded(instrument, span_exporter):
    middleware = _FakeAuthProtocol(_token_body())
    request = _request(
        **{"X-Auth-Token": "TOKENID", "X-Service-Token": "SVCTOKEN"}
    )

    middleware.process_request(request)

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes["keystonemiddleware.service_token.present"] is True
    # No service roles are configured, so the service token is not accepted.
    assert span.attributes["keystonemiddleware.service_token.valid"] is False
    assert span.attributes["keystonemiddleware.user_token.valid"] is True


def test_trace_context_extracted_from_headers(instrument, span_exporter):
    middleware = _FakeAuthProtocol(_token_body())
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    span_id = "b7ad6b7169203331"
    request = _request(
        **{
            "X-Auth-Token": "TOKENID",
            "traceparent": f"00-{trace_id}-{span_id}-01",
        }
    )

    middleware.process_request(request)

    span = span_exporter.get_finished_spans()[0]
    assert span.kind == SpanKind.SERVER
    assert format(span.context.trace_id, "032x") == trace_id
    assert format(span.parent.span_id, "016x") == span_id


def test_nested_under_active_span(instrument, span_exporter, tracer_provider):
    middleware = _FakeAuthProtocol(_token_body())
    request = _request(**{"X-Auth-Token": "TOKENID"})

    tracer = tracer_provider.get_tracer(__name__)
    with tracer.start_as_current_span("wsgi.server"):
        middleware.process_request(request)

    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    auth = spans["keystonemiddleware.authenticate"]
    server = spans["wsgi.server"]
    # An already-active span means the auth work is internal, nested under it.
    assert auth.kind == SpanKind.INTERNAL
    assert auth.parent.span_id == server.context.span_id
    assert auth.context.trace_id == server.context.trace_id


def test_fetch_error_sets_span_error(instrument, span_exporter):
    boom = RuntimeError("keystone unavailable")
    middleware = _FakeAuthProtocol(exc=boom)
    request = _request(**{"X-Auth-Token": "TOKENID"})

    with pytest.raises(RuntimeError, match="keystone unavailable"):
        middleware.process_request(request)

    span = span_exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_uninstrument_stops_tracing(instrument, span_exporter):
    instrument.uninstrument()

    middleware = _FakeAuthProtocol(_token_body())
    middleware.process_request(_request(**{"X-Auth-Token": "TOKENID"}))

    assert span_exporter.get_finished_spans() == ()
