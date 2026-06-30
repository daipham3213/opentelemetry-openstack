"""Log-record factory that injects OpenTelemetry trace context.

Every :class:`logging.LogRecord` is stamped with ``otelTraceID``,
``otelSpanID``, ``otelTraceSampled`` and ``otelServiceName`` so oslo.log's
``ContextFormatter`` format strings can reference them, e.g.::

    [DEFAULT]
    logging_default_format_string = %(asctime)s ... [trace_id=%(otelTraceID)s] %(message)s
"""

import logging
from typing import Callable, Optional

from opentelemetry.trace import (
    INVALID_SPAN,
    INVALID_SPAN_CONTEXT,
    format_span_id,
    format_trace_id,
    get_current_span,
)

LogHook = Callable[[object, logging.LogRecord], None]


def install_record_factory(
    provider, log_hook: Optional[LogHook] = None
) -> Callable:
    """Wrap the log-record factory to inject trace context; return the old one.

    The service name is resolved from ``provider.resource`` lazily on the first
    record and then cached, so it matches the resource the logs are exported
    with without needing the provider fully configured at instrument time.
    """
    old_factory = logging.getLogRecordFactory()
    service_name = None

    def resolve_service_name() -> str:
        nonlocal service_name
        if service_name is None:
            resource = getattr(provider, "resource", None)
            attributes = getattr(resource, "attributes", {})
            service_name = attributes.get("service.name", "")
        return service_name

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.otelServiceName = resolve_service_name()
        record.otelSpanID = record.otelTraceID = "0"
        record.otelTraceSampled = False

        span = get_current_span()
        ctx = span.get_span_context() if span != INVALID_SPAN else None
        if ctx and ctx != INVALID_SPAN_CONTEXT:
            record.otelSpanID = format_span_id(ctx.span_id)
            record.otelTraceID = format_trace_id(ctx.trace_id)
            record.otelTraceSampled = ctx.trace_flags.sampled
            if callable(log_hook):
                try:
                    log_hook(span, record)
                except Exception:  # pylint: disable=broad-except
                    pass
        return record

    logging.setLogRecordFactory(record_factory)
    return old_factory
