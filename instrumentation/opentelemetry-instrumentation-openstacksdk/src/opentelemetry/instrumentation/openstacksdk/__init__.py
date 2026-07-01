"""OpenTelemetry instrumentation for the OpenStack SDK (``openstacksdk``).

The `OpenStack SDK <https://docs.openstack.org/openstacksdk/>`_ exposes every
OpenStack service through a :class:`openstack.connection.Connection` whose
service proxies (``conn.compute``, ``conn.network``, ...) are subclasses of
:class:`openstack.proxy.Proxy`. Every REST call the SDK makes -- regardless of
which service or resource method is invoked -- funnels through
:meth:`openstack.proxy.Proxy.request`.

This instrumentor wraps that single choke point so each SDK REST call becomes a
``CLIENT`` span. The span carries the HTTP method, the resolved full URL and
response status, and the OpenStack ``service_type``/``region_name`` of the
proxy that issued it. The active W3C trace context is injected into the
outgoing request headers, so a trace started in the SDK continues on the
OpenStack service that handles the request (when that service is itself
instrumented).

Because the wrapper sits at the proxy layer, keystoneauth's own token/discovery
requests -- which go straight through the session rather than a proxy -- are
left untraced; only genuine SDK service calls produce spans.

Usage::

    from opentelemetry.instrumentation.openstacksdk import (
        OpenStackSDKInstrumentor,
    )

    OpenStackSDKInstrumentor().instrument()
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
from opentelemetry.instrumentation.openstacksdk.version import __version__
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
    from openstack.proxy import Proxy
    from requests import Response
except ImportError:
    Proxy = None
    Response = None

_LOG: logging.Logger = logging.getLogger(__name__)

_instruments: Collection[str] = ("openstacksdk",)

_PROXY_MODULE: str = "openstack.proxy"
_REQUEST_METHOD: str = "request"

# Non-standard attributes describing the OpenStack service the call targets.
_OPENSTACK_SERVICE_TYPE: str = "openstack.service_type"
_OPENSTACK_REGION_NAME: str = "openstack.region_name"

# The OpenStack global request id (``req-<uuid>``) correlates a single logical
# request across every service that handles it, so it is recorded under the
# semantic-convention conversation/correlation id attribute.
_CORRELATION_ID: str = messaging_attributes.MESSAGING_MESSAGE_CONVERSATION_ID

# Position of ``global_request_id`` in ``Proxy.request``'s signature, counting
# from the first argument after ``self`` (which ``wrapt`` strips into
# ``instance``): url, method, error_message, raise_exc, connect_retries,
# global_request_id.
_GLOBAL_REQUEST_ID_ARG: int = 5

# ``(wrapped, instance, args, kwargs)`` signature expected by ``wrapt``.
WrappedFunc = Callable[..., Any]
Wrapper = Callable[[WrappedFunc, Any, Tuple[Any, ...], Dict[str, Any]], Any]


def _http_method(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """Pull the HTTP ``method`` out of a ``Proxy.request`` call.

    ``Proxy.request(self, url, method, ...)`` is almost always called
    positionally by the SDK, but ``method`` may also arrive as a keyword.

    Args:
        args: Positional arguments passed to ``Proxy.request``.
        kwargs: Keyword arguments passed to ``Proxy.request``.

    Returns:
        The upper-cased HTTP method, or an empty string if none was supplied.
    """
    method: Optional[str] = args[1] if len(args) > 1 else kwargs.get("method")
    return (method or "").upper()


def _global_request_id(
    instance: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Optional[str]:
    """Resolve the OpenStack global request id for a ``Proxy.request`` call.

    Mirrors the SDK's own precedence: an explicit per-request
    ``global_request_id`` wins, otherwise the value configured on the
    connection (``conn._global_request_id``) is used.

    Args:
        instance: The bound :class:`openstack.proxy.Proxy` making the request.
        args: Positional arguments passed to ``Proxy.request``.
        kwargs: Keyword arguments passed to ``Proxy.request``.

    Returns:
        The ``req-<uuid>`` global request id, or ``None`` when none is set.
    """
    request_id: Optional[str] = kwargs.get("global_request_id")
    if request_id is None and len(args) > _GLOBAL_REQUEST_ID_ARG:
        request_id = args[_GLOBAL_REQUEST_ID_ARG]
    if request_id:
        return request_id

    get_connection = getattr(instance, "_get_connection", None)
    connection: Any = get_connection() if callable(get_connection) else None
    return getattr(connection, "_global_request_id", None)


def _http_url(args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    """Pull the HTTP ``url`` out of a ``Proxy.request`` call.

    ``Proxy.request(self, url, method, ...)`` is almost always called
    positionally by the SDK, but ``url`` may also arrive as a keyword.

    Args:
        args: Positional arguments passed to ``Proxy.request``.
        kwargs: Keyword arguments passed to ``Proxy.request``.

    Returns:
        The fully resolved URL, or an empty string if none was supplied.
    """
    url: Optional[str] = args[0] if len(args) > 0 else kwargs.get("url")
    return url or ""


def _record_response(span: Span, response: Response) -> None:
    """Record status/url attributes from the returned ``requests.Response``.

    ``Proxy.request`` defaults to ``raise_exc=False``, so HTTP error statuses
    come back as a normal response rather than an exception -- the span status
    is derived from the status code here.

    Args:
        span: The active client span to annotate.
        response: The ``requests.Response`` returned by ``Proxy.request`` (or
            ``None`` when the call yielded no response).
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

    # The fully resolved URL is only known after keystoneauth has looked up the
    # endpoint; read it back off the underlying prepared request.
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


def _request_wrapper(tracer: Tracer) -> Wrapper:
    """Build the ``wrapt`` wrapper for :meth:`openstack.proxy.Proxy.request`.

    Args:
        tracer: The tracer used to create the client span.

    Returns:
        A ``wrapt``-style wrapper closing over ``tracer``.
    """

    def wrapper(
        wrapped: WrappedFunc,
        instance: Proxy,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        if not is_instrumentation_enabled():
            return wrapped(*args, **kwargs)

        method: str = _http_method(args, kwargs)
        url: str = _http_url(args, kwargs)
        request_id: Optional[str] = _global_request_id(instance, args, kwargs)
        service_type: Optional[str] = getattr(instance, "service_type", None)
        region_name: Optional[str] = getattr(instance, "region_name", None)

        names = instance._extract_name(url=url, service_type=service_type)
        span_name: str = f"{method} {'.'.join(names)}"

        attributes: Attributes = {
            http_attributes.HTTP_REQUEST_METHOD: method,
            http_attributes.HTTP_ROUTE: url,
        }
        if service_type:
            attributes[_OPENSTACK_SERVICE_TYPE] = service_type
        if region_name:
            attributes[_OPENSTACK_REGION_NAME] = region_name
        if request_id:
            attributes[_CORRELATION_ID] = request_id

        with tracer.start_as_current_span(
            span_name, kind=SpanKind.CLIENT, attributes=attributes
        ) as span:
            # Inject the current (client-span) context into the request
            # headers. Copy the caller's mapping so we never mutate a dict the
            # caller still owns; keystoneauth merges these into what it sends.

            headers = dict(kwargs.get("headers", {}))
            propagate.inject(headers)

            kwargs["headers"] = headers
            response: Any = wrapped(*args, **kwargs)
            if span.is_recording():
                _record_response(span, response)
            return response

    return wrapper


class OpenStackSDKInstrumentor(BaseInstrumentor):
    """An instrumentor for the OpenStack SDK (``openstacksdk``)."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not is_instrumentation_enabled() or not Proxy:
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
            Proxy, _REQUEST_METHOD, _request_wrapper(tracer)
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        unwrap(Proxy, _REQUEST_METHOD)


__all__ = ["OpenStackSDKInstrumentor"]
