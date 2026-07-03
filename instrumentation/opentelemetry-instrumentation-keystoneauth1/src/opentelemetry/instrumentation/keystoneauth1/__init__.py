"""OpenTelemetry instrumentation for ``keystoneauth1``.

`keystoneauth1 <https://docs.openstack.org/keystoneauth/>`_ is the shared
authentication and HTTP library that virtually every OpenStack client sits on
top of. A :class:`keystoneauth1.session.Session` is the object that actually
talks HTTP: it resolves service endpoints from the catalog, attaches the auth
token, and issues the request. *Every* HTTP call the library makes -- a service
API call, a token request to Keystone, or a version-discovery probe -- funnels
through :meth:`keystoneauth1.session.Session.request`.

This instrumentor wraps that single choke point so each call becomes a
``CLIENT`` span carrying the HTTP method, the resolved full URL and response
status, the ``server.address``/``server.port`` it reached, and (when the caller
supplied an ``endpoint_filter``) the OpenStack ``service_type`` it targeted. The
active W3C trace context is injected into the outgoing request headers, so a
trace started in the client continues on the OpenStack service that handles the
request (when that service is itself instrumented).

Because it sits at the session layer -- *below* the OpenStack SDK's proxy -- this
instrumentor also captures the token and discovery requests that the SDK
instrumentation deliberately leaves untraced. When both are enabled the session
spans simply nest under the SDK's proxy spans.

Usage::

    from opentelemetry.instrumentation.keystoneauth1 import (
        KeystoneAuth1Instrumentor,
    )

    KeystoneAuth1Instrumentor().instrument()
"""

import logging
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Mapping,
    Optional,
    Tuple,
)
from urllib.parse import ParseResult, urlparse

import wrapt

from opentelemetry import propagate, trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.keystoneauth1.version import __version__
from opentelemetry.instrumentation.utils import (
    is_instrumentation_enabled,
    unwrap,
)
from opentelemetry.semconv._incubating.attributes import messaging_attributes
from opentelemetry.semconv.attributes import (
    http_attributes,
    server_attributes,
    url_attributes,
)
from opentelemetry.trace import Span, SpanKind, Tracer, TracerProvider
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.util.types import Attributes

try:
    from keystoneauth1.session import Session
except ImportError:
    Session = None

_LOG: logging.Logger = logging.getLogger(__name__)

_instruments: Collection[str] = ("keystoneauth1",)

_SESSION_MODULE: str = "keystoneauth1.session"
_REQUEST_METHOD: str = "request"

# Non-standard attribute describing the OpenStack service the call targets.
_OPENSTACK_SERVICE_TYPE: str = "openstack.service_type"

# The OpenStack global request id (``req-<uuid>``) correlates a single logical
# request across every service that handles it, so it is recorded under the
# semantic-convention conversation/correlation id attribute.
_CORRELATION_ID: str = messaging_attributes.MESSAGING_MESSAGE_CONVERSATION_ID

# ``(wrapped, instance, args, kwargs)`` signature expected by ``wrapt``.
WrappedFunc = Callable[..., Any]
Wrapper = Callable[[WrappedFunc, Any, Tuple[Any, ...], Dict[str, Any]], Any]


def _http_url(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """Pull the request ``url`` out of a ``Session.request`` call.

    ``Session.request(self, url, method, ...)`` is almost always called
    positionally, but ``url`` may also arrive as a keyword.

    Args:
        args: Positional arguments passed to ``Session.request``.
        kwargs: Keyword arguments passed to ``Session.request``.

    Returns:
        The requested URL (possibly a bare path), or an empty string if none
        was supplied.
    """
    url: Optional[str] = args[0] if len(args) > 0 else kwargs.get("url")
    return url or ""


def _http_method(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """Pull the HTTP ``method`` out of a ``Session.request`` call.

    ``Session.request(self, url, method, ...)`` is almost always called
    positionally, but ``method`` may also arrive as a keyword.

    Args:
        args: Positional arguments passed to ``Session.request``.
        kwargs: Keyword arguments passed to ``Session.request``.

    Returns:
        The upper-cased HTTP method, or an empty string if none was supplied.
    """
    method: Optional[str] = args[1] if len(args) > 1 else kwargs.get("method")
    return (method or "").upper()


def _service_type(kwargs: Mapping[str, Any]) -> Optional[str]:
    """Resolve the target ``service_type`` from a ``Session.request`` call.

    The service is described by the ``endpoint_filter`` mapping the caller (or
    the SDK adapter above it) passes so the session can find an endpoint in the
    catalog. Token and discovery requests carry no such filter.

    Args:
        kwargs: Keyword arguments passed to ``Session.request``.

    Returns:
        The OpenStack ``service_type``, or ``None`` when none was supplied.
    """
    endpoint_filter: Any = kwargs.get("endpoint_filter")
    if isinstance(endpoint_filter, Mapping):
        service_type = endpoint_filter.get("service_type")
        if service_type:
            return str(service_type)
    return None


def _record_url(span: Span, response: Any) -> None:
    """Record the resolved URL and server host/port from a response.

    The full URL is only known after keystoneauth has looked up the endpoint,
    so it is read back off the underlying prepared request rather than the
    (possibly relative) URL the caller passed in.

    Args:
        span: The active client span to annotate.
        response: The ``requests.Response`` (or ``None``) whose prepared
            request carries the resolved URL.
    """
    request: Any = getattr(response, "request", None)
    full_url: Optional[str] = (
        getattr(request, "url", None) if request is not None else None
    )
    if not full_url:
        return

    span.set_attribute(url_attributes.URL_FULL, full_url)
    parsed: ParseResult = urlparse(full_url)
    if parsed.hostname:
        span.set_attribute(server_attributes.SERVER_ADDRESS, parsed.hostname)
    if parsed.port:
        span.set_attribute(server_attributes.SERVER_PORT, parsed.port)


def _record_response(span: Span, response: Any) -> None:
    """Record status/url attributes from the returned ``requests.Response``.

    Used on the success path. ``Session.request`` raises for error statuses
    when ``raise_exc`` is left at its default of ``True``; when it is disabled
    an error status comes back as a normal response and the span status is
    derived from the status code here.

    Args:
        span: The active client span to annotate.
        response: The ``requests.Response`` returned by ``Session.request``.
    """
    if response is None:
        return

    status_code: Optional[int] = getattr(response, "status_code", None)
    if status_code is not None:
        span.set_attribute(
            http_attributes.HTTP_RESPONSE_STATUS_CODE, status_code
        )
        if status_code >= 400:
            span.set_status(Status(StatusCode.ERROR))

    _record_url(span, response)


def _record_error(span: Span, exc: BaseException) -> None:
    """Record status/url attributes from a raised keystoneauth ``HttpError``.

    A failed request surfaces as an exception that carries the HTTP status and
    the offending response, so the status code and resolved URL are recovered
    from it here. Transport-level errors (no response) simply leave those
    attributes unset; ``start_as_current_span`` records the exception itself.

    Args:
        span: The active client span to annotate.
        exc: The exception raised by ``Session.request``.
    """
    status_code: Any = getattr(exc, "http_status", None)
    if isinstance(status_code, int):
        span.set_attribute(
            http_attributes.HTTP_RESPONSE_STATUS_CODE, status_code
        )

    _record_url(span, getattr(exc, "response", None))


def _request_wrapper(tracer: Tracer) -> Wrapper:
    """Build the ``wrapt`` wrapper for :meth:`keystoneauth1.session.Session.request`.

    Args:
        tracer: The tracer used to create the client span.

    Returns:
        A ``wrapt``-style wrapper closing over ``tracer``.
    """

    def wrapper(
        wrapped: WrappedFunc,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        if not is_instrumentation_enabled():
            return wrapped(*args, **kwargs)

        method: str = _http_method(args, kwargs)
        url: str = _http_url(args, kwargs)
        service_type: Optional[str] = _service_type(kwargs)
        request_id: Optional[str] = kwargs.get("global_request_id")

        span_name: str = f"{method} {service_type}" if service_type else method

        attributes: Attributes = {
            http_attributes.HTTP_REQUEST_METHOD: method,
            http_attributes.HTTP_ROUTE: url,
        }
        if service_type:
            attributes[_OPENSTACK_SERVICE_TYPE] = service_type
        if request_id:
            attributes[_CORRELATION_ID] = request_id

        with tracer.start_as_current_span(
            span_name, kind=SpanKind.CLIENT, attributes=attributes
        ) as span:
            # Inject the current (client-span) context into the request
            # headers. Copy the caller's mapping so we never mutate a dict the
            # caller still owns; the session merges these into what it sends.
            headers = dict(kwargs.get("headers") or {})
            propagate.inject(headers)
            kwargs["headers"] = headers

            try:
                response: Any = wrapped(*args, **kwargs)
            except Exception as exc:
                if span.is_recording():
                    _record_error(span, exc)
                raise

            if span.is_recording():
                _record_response(span, response)
            return response

    return wrapper


class KeystoneAuth1Instrumentor(BaseInstrumentor):
    """An instrumentor for ``keystoneauth1`` sessions."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not is_instrumentation_enabled() or not Session:
            return

        tracer_provider: Optional[TracerProvider] = kwargs.get(
            "tracer_provider"
        )
        tracer: Tracer = trace.get_tracer(
            __name__,
            __version__,
            tracer_provider=tracer_provider,
            schema_url="https://opentelemetry.io/schemas/1.11.0",
        )

        wrapt.wrap_function_wrapper(
            Session, _REQUEST_METHOD, _request_wrapper(tracer)
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        if Session:
            unwrap(Session, _REQUEST_METHOD)


__all__ = ["KeystoneAuth1Instrumentor"]
