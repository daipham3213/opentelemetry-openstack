"""OpenTelemetry instrumentation for ``keystonemiddleware``.

`keystonemiddleware <https://docs.openstack.org/keystonemiddleware/>`_ provides
the ``auth_token`` WSGI middleware that virtually every OpenStack service mounts
at the front of its request pipeline. On every inbound request the middleware
takes the bearer token(s) carried in the request headers, validates them against
Keystone (hitting a local cache first), and -- when they check out -- decorates
the request with the authenticated identity (``X-User-Id``, ``X-Project-Id``,
``X-Roles`` and friends) before handing off to the service.

The instrumentor wraps two choke points, producing two nested spans::

    keystonemiddleware.request        (SERVER, spans the whole WSGI request)
      keystonemiddleware.authenticate (INTERNAL, the token validation step)
      <the service's own handler work: RPC sends, taskflow runs, ...>

**The request span.** ``BaseAuthProtocol.__call__`` is the WSGI entry point:
it validates the token and then invokes the downstream application. Wrapping it
opens a ``SERVER`` span that stays active for the *entire* request -- so every
span the service's handler creates while serving it (an ``oslo.messaging`` RPC
send, a ``taskflow`` flow, ...) nests under the same trace instead of starting a
disconnected one. Because ``auth_token`` is usually the first middleware to see
an inbound request, this is also where the distributed trace is *continued*: the
W3C trace context is extracted from the incoming request headers, so a trace
started by an instrumented client (the OpenStack SDK / keystoneauth1) carries on
into the service. If a span is already active -- e.g. an upstream WSGI/framework
instrumentor already opened the server span -- the request span nests under it as
``INTERNAL`` rather than opening a second server span.

**The authenticate span.** ``BaseAuthProtocol.process_request`` is the method
both the stock ``AuthProtocol`` and any subclass call to do the actual token
fetch-and-validate. Wrapping it records the outcome (whether a user and/or
service token was present and whether it was accepted) and, for accepted user
tokens, the resolved identity (user id/name, roles, project). During a normal
request it nests under the request span; called on its own it behaves as the
entry point (extracting context and opening a ``SERVER`` span itself).

Usage::

    from opentelemetry.instrumentation.keystonemiddleware import (
        KeystoneMiddlewareInstrumentor,
    )

    KeystoneMiddlewareInstrumentor().instrument()
"""

import logging
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Optional,
    Tuple,
)

import wrapt

from opentelemetry import context, propagate, trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.keystonemiddleware.version import (
    __version__,
)
from opentelemetry.instrumentation.utils import (
    is_instrumentation_enabled,
    unwrap,
)
from opentelemetry.propagators.textmap import Getter
from opentelemetry.semconv._incubating.attributes import user_attributes
from opentelemetry.semconv.attributes import (
    http_attributes,
    server_attributes,
    url_attributes,
)
from opentelemetry.trace import Span, SpanKind, Tracer, TracerProvider
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.util.types import Attributes

try:
    from keystonemiddleware.auth_token import BaseAuthProtocol
except ImportError:
    BaseAuthProtocol = None

_LOG: logging.Logger = logging.getLogger(__name__)

_instruments: Collection[str] = ("keystonemiddleware",)

_PROCESS_REQUEST: str = "process_request"
_CALL: str = "__call__"
_SPAN_NAME: str = "keystonemiddleware.authenticate"
_REQUEST_SPAN_NAME: str = "keystonemiddleware.request"

# Whether the request carried a user / service token, and whether the middleware
# accepted it. These live outside the semantic conventions because token
# validation is specific to the OpenStack auth_token middleware.
_USER_TOKEN_PRESENT: str = "keystonemiddleware.user_token.present"
_USER_TOKEN_VALID: str = "keystonemiddleware.user_token.valid"
_SERVICE_TOKEN_PRESENT: str = "keystonemiddleware.service_token.present"
_SERVICE_TOKEN_VALID: str = "keystonemiddleware.service_token.valid"

# The authenticated identity's OpenStack scope. There is no semantic convention
# for a project/domain, so these stay in the ``openstack`` namespace (matching
# the OpenStack SDK instrumentation).
_OPENSTACK_PROJECT_ID: str = "openstack.project_id"
_OPENSTACK_PROJECT_NAME: str = "openstack.project_name"
_OPENSTACK_DOMAIN_ID: str = "openstack.domain_id"

# ``(wrapped, instance, args, kwargs)`` signature expected by ``wrapt``.
WrappedFunc = Callable[..., Any]
Wrapper = Callable[[WrappedFunc, Any, Tuple[Any, ...], Dict[str, Any]], Any]


def _request_arg(
    args: Tuple[Any, ...], kwargs: Dict[str, Any]
) -> Optional[Any]:
    """Pull the ``request`` out of a ``process_request`` call.

    ``BaseAuthProtocol.process_request(self, request)`` is called positionally
    by the middleware, but tolerate a keyword just in case.

    Args:
        args: Positional arguments passed to ``process_request`` (``self``
            already stripped into ``instance`` by ``wrapt``).
        kwargs: Keyword arguments passed to ``process_request``.

    Returns:
        The ``_AuthTokenRequest`` being processed, or ``None`` if absent.
    """
    if args:
        return args[0]
    return kwargs.get("request")


def _parent_context(
    request: Any,
) -> Tuple[Optional[context.Context], SpanKind]:
    """Decide the parent context and kind for the authentication span.

    When a span is already active (an upstream WSGI instrumentor opened the
    server span, say) the authentication is internal work nested under it. When
    nothing is active this middleware is the trace's entry point, so the W3C
    context is extracted from the incoming request headers and the span becomes
    the ``SERVER`` side of the caller's client span.

    Args:
        request: The ``_AuthTokenRequest`` whose headers carry any inbound
            trace context.

    Returns:
        A ``(context, kind)`` pair: ``context`` is ``None`` to reuse the active
        context, or an extracted context to continue a remote trace.
    """
    current: Span = trace.get_current_span()
    if current.get_span_context().is_valid:
        return None, SpanKind.INTERNAL

    headers: Any = getattr(request, "headers", None)
    if headers is None:
        return None, SpanKind.SERVER
    return propagate.extract(carrier=headers), SpanKind.SERVER


class _WSGIEnvironGetter(Getter):
    """Read propagation headers out of a WSGI ``environ`` mapping.

    WSGI stores request headers under ``HTTP_``-prefixed, upper-cased,
    underscore-separated keys (``traceparent`` -> ``HTTP_TRACEPARENT``), so the
    default header-name getter cannot see them.
    """

    def get(self, carrier: Dict[str, Any], key: str) -> Optional[list]:
        value = carrier.get("HTTP_" + key.upper().replace("-", "_"))
        return [value] if value is not None else None

    def keys(self, carrier: Dict[str, Any]) -> list:
        return [
            key[5:].replace("_", "-").lower()
            for key in carrier
            if key.startswith("HTTP_")
        ]


_ENVIRON_GETTER: _WSGIEnvironGetter = _WSGIEnvironGetter()


def _environ_parent_context(
    environ: Any,
) -> Tuple[Optional[context.Context], SpanKind]:
    """Decide the parent context and kind for the request span.

    Mirrors :func:`_parent_context` but reads the inbound trace context from a
    WSGI ``environ`` rather than a request's headers. When a span is already
    active (an upstream WSGI/framework instrumentor opened the server span) the
    request span nests under it as ``INTERNAL``; otherwise this middleware is
    the trace's entry point and opens a ``SERVER`` span from the extracted
    context.

    Args:
        environ: The WSGI environment of the incoming request.

    Returns:
        A ``(context, kind)`` pair.
    """
    current: Span = trace.get_current_span()
    if current.get_span_context().is_valid:
        return None, SpanKind.INTERNAL

    if not isinstance(environ, dict):
        return None, SpanKind.SERVER
    return (
        propagate.extract(carrier=environ, getter=_ENVIRON_GETTER),
        SpanKind.SERVER,
    )


def _request_attributes(environ: Any) -> Attributes:
    """Build the ``http.*``/``url.*``/``server.*`` attributes for the request.

    Args:
        environ: The WSGI environment of the incoming request.

    Returns:
        The attribute mapping for the request span (only present fields).
    """
    if not isinstance(environ, dict):
        return {}

    attributes: Dict[str, Any] = {}
    method: Any = environ.get("REQUEST_METHOD")
    if method:
        attributes[http_attributes.HTTP_REQUEST_METHOD] = str(method)
    path: Any = environ.get("PATH_INFO")
    if path:
        attributes[url_attributes.URL_PATH] = str(path)
    scheme: Any = environ.get("wsgi.url_scheme")
    if scheme:
        attributes[url_attributes.URL_SCHEME] = str(scheme)
    host: Any = environ.get("SERVER_NAME")
    if host:
        attributes[server_attributes.SERVER_ADDRESS] = str(host)
    port: Any = environ.get("SERVER_PORT")
    try:
        if port is not None:
            attributes[server_attributes.SERVER_PORT] = int(port)
    except (TypeError, ValueError):
        pass
    return attributes


def _status_code(status_line: Any) -> Optional[int]:
    """Parse the numeric status out of a WSGI ``"200 OK"`` status line.

    Args:
        status_line: The status string passed to ``start_response``.

    Returns:
        The integer status code, or ``None`` when it could not be parsed.
    """
    if not isinstance(status_line, str):
        return None
    code, _, _ = status_line.strip().partition(" ")
    try:
        return int(code)
    except ValueError:
        return None


def _token_present(request: Any, attr: str) -> bool:
    """Whether the request carried the named token, tolerating access errors.

    Args:
        request: The ``_AuthTokenRequest`` being processed.
        attr: The request property to read (``user_token`` or
            ``service_token``).

    Returns:
        ``True`` when the token header was present, ``False`` otherwise.
    """
    try:
        return bool(getattr(request, attr, None))
    except Exception:  # noqa: BLE001 - never let attribute quirks break auth
        return False


def _token_valid(request: Any, attr: str) -> Optional[bool]:
    """Whether the middleware accepted the named token.

    The ``*_token_valid`` properties read a header the middleware only sets when
    the corresponding token was present, so reading them otherwise raises. The
    caller guards on presence; this simply swallows any stray lookup error.

    Args:
        request: The ``_AuthTokenRequest`` being processed.
        attr: The request property to read (``user_token_valid`` or
            ``service_token_valid``).

    Returns:
        The validity flag, or ``None`` when it could not be determined.
    """
    try:
        return bool(getattr(request, attr))
    except Exception:  # noqa: BLE001 - header may be unset; treat as unknown
        return None


def _record_identity(span: Span, request: Any) -> None:
    """Record the authenticated identity resolved for a valid user token.

    Reads the :class:`keystoneauth1.access.AccessInfo` the middleware attached
    to ``request.token_auth.user`` and maps the identity onto ``user.*``
    semantic-convention attributes plus ``openstack.*`` scope attributes. Each
    field is best-effort: a missing or oddly-typed value is simply skipped.

    Args:
        span: The active authentication span to annotate.
        request: The processed ``_AuthTokenRequest``.
    """
    token_auth: Any = getattr(request, "token_auth", None)
    auth_ref: Any = getattr(token_auth, "user", None)
    if auth_ref is None:
        return

    _set_str(span, user_attributes.USER_ID, getattr(auth_ref, "user_id", None))
    _set_str(
        span, user_attributes.USER_NAME, getattr(auth_ref, "username", None)
    )
    _set_str(
        span, _OPENSTACK_PROJECT_ID, getattr(auth_ref, "project_id", None)
    )
    _set_str(
        span, _OPENSTACK_PROJECT_NAME, getattr(auth_ref, "project_name", None)
    )
    _set_str(span, _OPENSTACK_DOMAIN_ID, getattr(auth_ref, "domain_id", None))

    role_names: Any = getattr(auth_ref, "role_names", None)
    if role_names:
        span.set_attribute(
            user_attributes.USER_ROLES, tuple(str(r) for r in role_names)
        )


def _set_str(span: Span, key: str, value: Any) -> None:
    """Set ``key`` to ``value`` on ``span`` only when there is a value.

    Args:
        span: The span to annotate.
        key: The attribute key.
        value: The candidate value; skipped when falsy.
    """
    if value:
        span.set_attribute(key, str(value))


def _record_request(span: Span, request: Any) -> None:
    """Record token presence/validity and identity onto the span.

    Args:
        span: The active authentication span to annotate.
        request: The processed ``_AuthTokenRequest``.
    """
    if request is None:
        return

    user_present: bool = _token_present(request, "user_token")
    span.set_attribute(_USER_TOKEN_PRESENT, user_present)
    user_valid: Optional[bool] = None
    if user_present:
        user_valid = _token_valid(request, "user_token_valid")
        if user_valid is not None:
            span.set_attribute(_USER_TOKEN_VALID, user_valid)

    service_present: bool = _token_present(request, "service_token")
    span.set_attribute(_SERVICE_TOKEN_PRESENT, service_present)
    if service_present:
        service_valid: Optional[bool] = _token_valid(
            request, "service_token_valid"
        )
        if service_valid is not None:
            span.set_attribute(_SERVICE_TOKEN_VALID, service_valid)

    if user_valid:
        _record_identity(span, request)


def _call_wrapper(tracer: Tracer) -> Wrapper:
    """Build the ``wrapt`` wrapper for ``BaseAuthProtocol.__call__``.

    ``__call__(environ, start_response)`` is the WSGI entry point: it validates
    the token and then invokes the downstream application. Wrapping it opens a
    request-scoping span that stays active across the whole request, so every
    span the service's handler creates while serving it nests under the same
    trace. The span continues an inbound distributed trace extracted from the
    request headers, and records basic HTTP attributes plus the response status.

    Args:
        tracer: The tracer used to create the request span.

    Returns:
        A ``wrapt``-style wrapper closing over ``tracer``.
    """

    def wrapper(
        wrapped: WrappedFunc,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        # Only the standard WSGI ``(environ, start_response)`` positional call
        # is instrumented; anything else is passed straight through untouched.
        if not is_instrumentation_enabled() or len(args) != 2:
            return wrapped(*args, **kwargs)

        environ, start_response = args
        parent, kind = _environ_parent_context(environ)
        attributes: Attributes = _request_attributes(environ)

        with tracer.start_as_current_span(
            _REQUEST_SPAN_NAME,
            context=parent,
            kind=kind,
            attributes=attributes,
        ) as span:
            captured: Dict[str, Any] = {}

            def traced_start_response(status, headers, exc_info=None):
                captured["status"] = status
                return start_response(status, headers, exc_info)

            try:
                result: Any = wrapped(environ, traced_start_response)
            except Exception as exc:
                if span.is_recording():
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

            if span.is_recording():
                _record_status(span, captured.get("status"))
            return result

    return wrapper


def _record_status(span: Span, status_line: Any) -> None:
    """Record the response status on the request span.

    A ``5xx`` marks the span as an error (a server-side failure); ``4xx`` is a
    client error and leaves the status unset, per HTTP server conventions.

    Args:
        span: The request span to annotate.
        status_line: The status line captured from ``start_response``.
    """
    status_code: Optional[int] = _status_code(status_line)
    if status_code is None:
        return
    span.set_attribute(http_attributes.HTTP_RESPONSE_STATUS_CODE, status_code)
    if status_code >= 500:
        span.set_status(Status(StatusCode.ERROR))


def _process_request_wrapper(tracer: Tracer) -> Wrapper:
    """Build the ``wrapt`` wrapper for ``BaseAuthProtocol.process_request``.

    Args:
        tracer: The tracer used to create the authentication span.

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

        request: Optional[Any] = _request_arg(args, kwargs)
        parent, kind = _parent_context(request)

        with tracer.start_as_current_span(
            _SPAN_NAME, context=parent, kind=kind
        ) as span:
            try:
                result: Any = wrapped(*args, **kwargs)
            except Exception as exc:
                # A token that simply fails validation is caught inside
                # process_request and surfaces as ``*_token_valid = False``;
                # anything raised here (Keystone unreachable, endpoint lookup
                # failure, ...) is a genuine error for the span.
                if span.is_recording():
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    _record_request(span, request)
                raise

            if span.is_recording():
                _record_request(span, request)
            return result

    return wrapper


class KeystoneMiddlewareInstrumentor(BaseInstrumentor):
    """An instrumentor for ``keystonemiddleware``'s ``auth_token`` middleware."""

    # The pristine ``__call__`` captured at instrument time. ``__call__`` is a
    # ``webob.dec.wsgify`` descriptor, and unwrapping a wrapped descriptor via
    # ``unwrap`` rebinds it rather than restoring the original, so it is saved
    # and reinstated by hand instead. Class-level (not in ``__init__``) because
    # ``BaseInstrumentor`` is a singleton.
    _original_call = None

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not is_instrumentation_enabled() or not BaseAuthProtocol:
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

        # Request-scoping SERVER span for the whole WSGI request, then the
        # nested INTERNAL authentication span within it.
        type(self)._original_call = BaseAuthProtocol.__dict__.get(_CALL)
        wrapt.wrap_function_wrapper(
            BaseAuthProtocol,
            _CALL,
            _call_wrapper(tracer),
        )
        wrapt.wrap_function_wrapper(
            BaseAuthProtocol,
            _PROCESS_REQUEST,
            _process_request_wrapper(tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        if not BaseAuthProtocol:
            return
        if type(self)._original_call is not None:
            BaseAuthProtocol.__call__ = type(self)._original_call
            type(self)._original_call = None
        unwrap(BaseAuthProtocol, _PROCESS_REQUEST)


__all__ = ["KeystoneMiddlewareInstrumentor"]
