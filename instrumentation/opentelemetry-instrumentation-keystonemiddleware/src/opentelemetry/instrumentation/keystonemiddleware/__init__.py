"""OpenTelemetry instrumentation for ``keystonemiddleware``.

`keystonemiddleware <https://docs.openstack.org/keystonemiddleware/>`_ provides
the ``auth_token`` WSGI middleware that virtually every OpenStack service mounts
at the front of its request pipeline. On every inbound request the middleware
takes the bearer token(s) carried in the request headers, validates them against
Keystone (hitting a local cache first), and -- when they check out -- decorates
the request with the authenticated identity (``X-User-Id``, ``X-Project-Id``,
``X-Roles`` and friends) before handing off to the service.

All of that funnels through
:meth:`keystonemiddleware.auth_token.BaseAuthProtocol.process_request`, the
method both the stock ``AuthProtocol`` and any custom subclass call to do the
actual token fetch-and-validate. This instrumentor wraps that single choke point
so each authentication becomes a span carrying the outcome (whether a user and/or
service token was present and whether it was accepted) and, for accepted user
tokens, the resolved identity (user id/name, roles, project).

Because the ``auth_token`` middleware is usually the first thing to see an
inbound request, it is also the natural place to *continue* a distributed trace:
when no span is already active the instrumentor extracts the W3C trace context
from the incoming request headers and starts a ``SERVER`` span, so a trace
started by an instrumented client (for example the OpenStack SDK) carries on into
the service. When a span is already active -- e.g. an upstream WSGI instrumentor
already opened the server span -- the authentication span is nested under it as
an ``INTERNAL`` span instead.

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
from opentelemetry.semconv._incubating.attributes import user_attributes
from opentelemetry.trace import Span, SpanKind, Tracer, TracerProvider
from opentelemetry.trace.status import Status, StatusCode

try:
    from keystonemiddleware.auth_token import BaseAuthProtocol
except ImportError:
    BaseAuthProtocol = None

_LOG: logging.Logger = logging.getLogger(__name__)

_instruments: Collection[str] = ("keystonemiddleware",)

_PROCESS_REQUEST: str = "process_request"
_SPAN_NAME: str = "keystonemiddleware.authenticate"

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

        wrapt.wrap_function_wrapper(
            BaseAuthProtocol,
            _PROCESS_REQUEST,
            _process_request_wrapper(tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        if BaseAuthProtocol:
            unwrap(BaseAuthProtocol, _PROCESS_REQUEST)


__all__ = ["KeystoneMiddlewareInstrumentor"]
