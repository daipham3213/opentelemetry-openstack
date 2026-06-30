"""OpenTelemetry instrumentation for oslo.log.

Exports OpenStack service logs to the OpenTelemetry logs pipeline (and onward
to an OTLP collector) while keeping them correlated with traces. On
``instrument()`` it:

* stamps ``otelTraceID``, ``otelSpanID``, ``otelTraceSampled`` and
  ``otelServiceName`` onto every record (see :mod:`._factory`) so oslo.log's
  ``ContextFormatter`` format strings can reference them;
* installs an :class:`~.handler.OsloLogHandler` on oslo.log's root logger so
  records are exported through the configured ``logger_provider``, with oslo
  request context projected onto the record attributes.

``oslo_log.log.setup`` drops every root handler and rebuilds them from
oslo.config, so ``setup`` is wrapped while instrumented to re-attach the
exporting handler afterwards.

Note: the handler is attached to oslo.log's root logger, not the stdlib root.
Non-oslo libraries that log straight to the stdlib root are the domain of
``opentelemetry-instrumentation-logging``.

Usage::

    from opentelemetry.instrumentation.oslo_log import OsloLogInstrumentor

    OsloLogInstrumentor().instrument()
"""

import logging
from os import environ
from typing import Collection, Optional, Union

import wrapt

from opentelemetry._logs import get_logger_provider
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.logging import (
    DEFAULT_LOGGING_FORMAT,
    LEVELS,
)
from opentelemetry.instrumentation.logging.environment_variables import (
    OTEL_PYTHON_LOG_CODE_ATTRIBUTES,
    OTEL_PYTHON_LOG_CORRELATION,
    OTEL_PYTHON_LOG_FORMAT,
    OTEL_PYTHON_LOG_HANDLER_LEVEL,
    OTEL_PYTHON_LOG_LEVEL,
)
from opentelemetry.instrumentation.oslo_log._factory import (
    install_record_factory,
)
from opentelemetry.instrumentation.oslo_log.handler import OsloLogHandler
from opentelemetry.instrumentation.oslo_log.version import __version__
from opentelemetry.instrumentation.utils import unwrap

try:
    from oslo_log import log as oslo_logging
except ImportError:
    oslo_logging = None

_instruments = ("oslo.log",)


def _resolve_log_level(
    log_level: Optional[Union[int, str]],
) -> Optional[int]:
    """Coerce a textual log level (e.g. ``"INFO"``) to its numeric value."""
    if log_level is None or isinstance(log_level, int):
        return log_level
    numeric_level = logging.getLevelName(log_level.upper().strip())
    return numeric_level if isinstance(numeric_level, int) else None


def _env_flag(name: str, default: str) -> bool:
    return environ.get(name, default).strip().lower() == "true"


def _oslo_root_logger() -> logging.Logger:
    """Return the logger oslo.log manages handlers on."""
    return oslo_logging.getLogger(None).logger


class OsloLogInstrumentor(BaseInstrumentor):
    """An oslo.log instrumentor that exports records and correlates traces."""

    # Class-level defaults; assigned per run in ``_instrument``. Stored here
    # rather than in ``__init__`` because ``BaseInstrumentor`` is a singleton
    # and a re-running ``__init__`` would clobber live state.
    _handler = None
    _old_factory = None

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        """Instrument oslo.log to export records and correlate traces.

        Args:
            logger_provider: ``LoggerProvider`` used to export records and resolve
                the service name. Defaults to the global provider.
            set_logging_format: When True, calls ``logging.basicConfig()``.
                Defaults to the ``OTEL_PYTHON_LOG_CORRELATION`` env var.
            logging_format / log_level: format and level for ``set_logging_format``.
            log_hook: Callable ``(span, record)`` invoked for records emitted
                within an active span, to attach custom attributes.
            log_code_attributes: When True, attaches code attributes (file, func,
                line) to exported records.
            log_handler_level: Level applied to the installed handler.
            map_oslo_context: When True (default), maps oslo request context
                (request id, user/project ids, ...) onto exported attributes.
        """
        if oslo_logging is None:
            return  # oslo.log is not available

        provider = kwargs.get("logger_provider") or get_logger_provider()

        if kwargs.get(
            "set_logging_format",
            _env_flag(OTEL_PYTHON_LOG_CORRELATION, "false"),
        ):
            log_format = (
                kwargs.get("logging_format")
                or environ.get(OTEL_PYTHON_LOG_FORMAT)
                or DEFAULT_LOGGING_FORMAT
            )
            log_level = kwargs.get("log_level") or LEVELS.get(
                environ.get(OTEL_PYTHON_LOG_LEVEL)
            )
            logging.basicConfig(
                format=log_format,
                level=_resolve_log_level(log_level) or logging.INFO,
            )

        self._old_factory = install_record_factory(
            provider, log_hook=kwargs.get("log_hook")
        )

        self._handler = OsloLogHandler(
            level=_resolve_log_level(
                kwargs.get("log_handler_level")
                or environ.get(OTEL_PYTHON_LOG_HANDLER_LEVEL)
            )
            or logging.NOTSET,
            logger_provider=provider,
            log_code_attributes=kwargs.get(
                "log_code_attributes",
                _env_flag(OTEL_PYTHON_LOG_CODE_ATTRIBUTES, "false"),
            ),
            map_oslo_context=kwargs.get("map_oslo_context", True),
        )
        self._attach_handler()
        wrapt.wrap_function_wrapper(oslo_logging, "setup", self._guarded_setup)

    def _uninstrument(self, **kwargs):
        if self._old_factory is not None:
            logging.setLogRecordFactory(self._old_factory)
            self._old_factory = None
        unwrap(oslo_logging, "setup")
        if self._handler is not None:
            _oslo_root_logger().removeHandler(self._handler)
            self._handler = None

    def _attach_handler(self) -> None:
        root = _oslo_root_logger()
        if self._handler is not None and self._handler not in root.handlers:
            root.addHandler(self._handler)

    def _guarded_setup(self, wrapped, _instance, args, kwargs):
        # oslo's setup() drops every root handler and rebuilds them from
        # config; re-attach ours once it has finished.
        result = wrapped(*args, **kwargs)
        self._attach_handler()
        return result


__all__ = [
    "DEFAULT_LOGGING_FORMAT",
    "OsloLogInstrumentor",
    "__version__",
]
