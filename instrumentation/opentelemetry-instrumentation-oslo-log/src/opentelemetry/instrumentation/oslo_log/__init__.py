"""OpenTelemetry instrumentation for oslo.log.

oslo.log builds on top of the standard library :mod:`logging` module, so trace
context can be injected into log records the same way the upstream
``opentelemetry-instrumentation-logging`` package does it: by wrapping the
record factory. This instrumentor reuses that approach but tailors it for
oslo.log:

* the OpenTelemetry attributes (``otelTraceID``, ``otelSpanID``,
  ``otelTraceSampled`` and ``otelServiceName``) are always added to every
  record so they are available to oslo.log's ``ContextFormatter`` format
  strings, regardless of whether ``set_logging_format`` is used;
* the service name is resolved from the (optional) ``logger_provider`` so it
  matches the resource the logs are exported with;
* a ``LoggingHandler`` backed by that same ``logger_provider`` is installed on
  the root logger so oslo.log records are exported through the OpenTelemetry
  logs pipeline. This can be disabled with ``enable_log_auto_instrumentation``
  (or the ``OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION`` environment variable).
  Because ``oslo_log.log.setup`` rebuilds the root logger's handlers, it is
  wrapped while instrumented so the exporting handler is re-added afterwards;
  this means the instrumentor keeps working even if ``setup`` runs afterwards.

To surface the injected fields in oslo.log output, reference them from
``logging_context_format_string`` / ``logging_default_format_string`` in your
oslo.config, e.g.::

    [DEFAULT]
    logging_default_format_string = %(asctime)s %(levelname)s %(name)s [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(message)s

Usage::

    from opentelemetry.instrumentation.oslo_log import OsloLogInstrumentor

    OsloLogInstrumentor().instrument()
"""

import logging
from functools import wraps
from os import environ
from typing import Collection, Optional, Union

from opentelemetry._logs import get_logger_provider
from opentelemetry.instrumentation.logging import (
    DEFAULT_LOGGING_FORMAT,
    LEVELS,
    LoggingInstrumentor,
)
from opentelemetry.instrumentation.logging.environment_variables import (
    OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION,
    OTEL_PYTHON_LOG_CODE_ATTRIBUTES,
    OTEL_PYTHON_LOG_CORRELATION,
    OTEL_PYTHON_LOG_FORMAT,
    OTEL_PYTHON_LOG_HANDLER_LEVEL,
    OTEL_PYTHON_LOG_LEVEL,
)
from opentelemetry.instrumentation.logging.handler import (
    _setup_logging_handler,
)
from opentelemetry.instrumentation.oslo_log.version import __version__
from opentelemetry.trace import (
    INVALID_SPAN,
    INVALID_SPAN_CONTEXT,
    format_span_id,
    format_trace_id,
    get_current_span,
)

try:
    from oslo_log import log as oslo_logging
except ImportError:
    oslo_logging = None

_instruments = ("oslo.log",)

# Original ``oslo_log.log.setup`` saved while the setup guard is installed.
_original_setup = None


def _resolve_log_level(
    log_level: Optional[Union[int, str]],
) -> Optional[int]:
    """Coerce a textual log level (e.g. ``"INFO"``) to its numeric value."""
    if log_level is None or isinstance(log_level, int):
        return log_level
    numeric_level = logging.getLevelName(log_level.upper().strip())
    return numeric_level if isinstance(numeric_level, int) else None


def _install_setup_guard():
    """Re-add the OTLP handler whenever ``oslo_log.log.setup`` rebuilds them.

    ``oslo_log.log.setup`` removes every handler from the root logger and
    re-adds the ones configured through oslo.config. If it runs after we have
    instrumented, our ``LoggingHandler`` would be silently dropped. Wrapping
    ``setup`` lets us restore the handler once oslo.log has finished.
    """
    global _original_setup  # pylint: disable=global-statement
    if _original_setup is not None:
        return

    original_setup = oslo_logging.setup

    @wraps(original_setup)
    def guarded_setup(*args, **kwargs):
        result = original_setup(*args, **kwargs)
        handler = OsloLogInstrumentor._logging_handler
        if handler is not None:
            root = logging.getLogger()
            if handler not in root.handlers:
                root.addHandler(handler)
        return result

    _original_setup = original_setup
    oslo_logging.setup = guarded_setup


def _remove_setup_guard():
    global _original_setup  # pylint: disable=global-statement
    if _original_setup is not None:
        oslo_logging.setup = _original_setup
        _original_setup = None


class OsloLogInstrumentor(LoggingInstrumentor):
    """An oslo.log instrumentor that injects trace context into log records.

    Args:
        logger_provider: ``LoggerProvider`` used to resolve the service name
            recorded as ``otelServiceName``. Defaults to the global provider.
        set_logging_format: When True, calls ``logging.basicConfig()`` to set a
            logging format and level. Defaults to the value of the
            ``OTEL_PYTHON_LOG_CORRELATION`` environment variable.
        logging_format: Format string used when ``set_logging_format`` is True.
        log_level: Logging level used when ``set_logging_format`` is True.
            Accepts either a numeric level (``logging.INFO``) or its name
            (``"INFO"``).
        log_hook: Callable ``(span, record)`` invoked for every record emitted
            within an active span, allowing custom attributes to be attached.
        enable_log_auto_instrumentation: When True (the default), installs a
            ``LoggingHandler`` on the root logger that exports records through
            the ``logger_provider``. Defaults to the value of the
            ``OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION`` environment variable.
        log_code_attributes: When True, attaches code attributes (file path,
            function name, line number) to exported records.
        log_handler_level: Level applied to the installed ``LoggingHandler``.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        if oslo_logging is None:
            return  # oslo.log is not available

        # Idempotent: a second instrument() call must not wrap the factory
        # again or clobber the saved original factory.
        if OsloLogInstrumentor._old_factory is not None:
            return

        provider = kwargs.get("logger_provider") or get_logger_provider()
        OsloLogInstrumentor._log_hook = kwargs.get("log_hook", None)

        set_logging_format = kwargs.get(
            "set_logging_format",
            environ.get(OTEL_PYTHON_LOG_CORRELATION, "false").lower()
            == "true",
        )

        if set_logging_format:
            log_format = kwargs.get(
                "logging_format", environ.get(OTEL_PYTHON_LOG_FORMAT, None)
            )
            log_format = log_format or DEFAULT_LOGGING_FORMAT

            log_level = kwargs.get("log_level")
            if log_level is None:
                log_level = LEVELS.get(environ.get(OTEL_PYTHON_LOG_LEVEL))
            log_level = _resolve_log_level(log_level) or logging.INFO

            logging.basicConfig(format=log_format, level=log_level)

        # Service name is resolved lazily on first use and then cached.
        service_name = None

        old_factory = logging.getLogRecordFactory()
        OsloLogInstrumentor._old_factory = old_factory

        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)

            record.otelSpanID = "0"
            record.otelTraceID = "0"
            record.otelTraceSampled = False

            nonlocal service_name
            if service_name is None:
                resource = getattr(provider, "resource", None)
                if resource:
                    service_name = (
                        resource.attributes.get("service.name") or ""
                    )
                else:
                    service_name = ""
            record.otelServiceName = service_name

            span = get_current_span()
            if span != INVALID_SPAN:
                ctx = span.get_span_context()
                if ctx != INVALID_SPAN_CONTEXT:
                    record.otelSpanID = format_span_id(ctx.span_id)
                    record.otelTraceID = format_trace_id(ctx.trace_id)
                    record.otelTraceSampled = ctx.trace_flags.sampled

                    if callable(OsloLogInstrumentor._log_hook):
                        try:
                            OsloLogInstrumentor._log_hook(span, record)
                        except Exception:  # pylint: disable=broad-except
                            pass

            return record

        logging.setLogRecordFactory(record_factory)

        # Install a LoggingHandler so oslo.log records (which propagate to the
        # root logger) are exported through the OpenTelemetry logs pipeline.
        enable_handler = kwargs.get(
            "enable_log_auto_instrumentation",
            environ.get(OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION, "true")
            .strip()
            .lower()
            == "true",
        )
        if enable_handler:
            log_code_attributes = kwargs.get(
                "log_code_attributes",
                environ.get(OTEL_PYTHON_LOG_CODE_ATTRIBUTES, "false")
                .strip()
                .lower()
                == "true",
            )
            handler_level = kwargs.get(
                "log_handler_level",
                _resolve_log_level(environ.get(OTEL_PYTHON_LOG_HANDLER_LEVEL)),
            )
            OsloLogInstrumentor._logging_handler = _setup_logging_handler(
                logger_provider=provider,
                log_code_attributes=log_code_attributes,
                level=handler_level,
            )
            _install_setup_guard()

    def _uninstrument(self, **kwargs):
        if OsloLogInstrumentor._old_factory:
            logging.setLogRecordFactory(OsloLogInstrumentor._old_factory)
            OsloLogInstrumentor._old_factory = None

        _remove_setup_guard()

        if OsloLogInstrumentor._logging_handler:
            logging.getLogger().removeHandler(
                OsloLogInstrumentor._logging_handler
            )
            OsloLogInstrumentor._logging_handler = None


__all__ = [
    "DEFAULT_LOGGING_FORMAT",
    "OsloLogInstrumentor",
    "__version__",
]
