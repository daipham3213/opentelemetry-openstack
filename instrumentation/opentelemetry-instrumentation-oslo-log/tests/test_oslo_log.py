import logging

import pytest
from oslo_config import cfg
from oslo_log import log as oslo_logging

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.instrumentation.oslo_log import OsloLogInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import format_span_id, format_trace_id


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture(autouse=True)
def restore_logging():
    original_factory = logging.getLogRecordFactory()
    original_handlers = list(logging.root.handlers)
    original_level = logging.root.level
    OsloLogInstrumentor().uninstrument()

    yield

    OsloLogInstrumentor().uninstrument()
    logging.setLogRecordFactory(original_factory)
    logging.root.handlers = original_handlers
    logging.root.setLevel(original_level)


@pytest.fixture
def log_records():
    handler = ListHandler()
    logger = logging.getLogger("oslo-log-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    yield handler.records

    logger.handlers = []


def emit_record(message="message"):
    logging.getLogger("oslo-log-test").info(message)


def logger_provider(service_name="test-service"):
    return LoggerProvider(
        resource=Resource.create({SERVICE_NAME: service_name})
    )


def tracer_provider():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    return provider


def test_instrumentation_dependencies():
    assert OsloLogInstrumentor().instrumentation_dependencies() == (
        "oslo.log",
    )


def test_log_record_has_default_otel_fields_without_active_span(log_records):
    OsloLogInstrumentor().instrument(
        logger_provider=logger_provider("default-service")
    )

    emit_record()

    record = log_records[0]
    assert record.otelSpanID == "0"
    assert record.otelTraceID == "0"
    assert record.otelTraceSampled is False
    assert record.otelServiceName == "default-service"


def test_log_record_has_active_span_context(log_records):
    provider = tracer_provider()
    tracer = provider.get_tracer(__name__)
    OsloLogInstrumentor().instrument(
        logger_provider=logger_provider("span-service")
    )

    with tracer.start_as_current_span("span") as span:
        emit_record()

    span_context = span.get_span_context()
    record = log_records[0]
    assert record.otelSpanID == format_span_id(span_context.span_id)
    assert record.otelTraceID == format_trace_id(span_context.trace_id)
    assert record.otelTraceSampled is True
    assert record.otelServiceName == "span-service"


def test_instrument_twice_does_not_wrap_factory_twice(log_records):
    instrumentor = OsloLogInstrumentor()
    instrumentor.instrument(logger_provider=logger_provider())
    factory_after_first_instrument = logging.getLogRecordFactory()

    instrumentor.instrument(logger_provider=logger_provider("ignored"))

    assert logging.getLogRecordFactory() is factory_after_first_instrument
    emit_record()
    assert log_records[0].otelServiceName == "test-service"


def test_uninstrument_restores_original_factory(log_records):
    original_factory = logging.getLogRecordFactory()
    instrumentor = OsloLogInstrumentor()
    instrumentor.instrument(logger_provider=logger_provider())

    instrumentor.uninstrument()

    assert logging.getLogRecordFactory() is original_factory
    emit_record()
    assert not hasattr(log_records[0], "otelTraceID")


def test_set_logging_format_configures_basic_config(monkeypatch):
    basic_config_kwargs = {}

    def basic_config(**kwargs):
        basic_config_kwargs.update(kwargs)

    monkeypatch.setattr(logging, "basicConfig", basic_config)

    OsloLogInstrumentor().instrument(
        logger_provider=logger_provider("format-service"),
        set_logging_format=True,
        logging_format="trace_id=%(otelTraceID)s service=%(otelServiceName)s %(message)s",
        log_level="INFO",
    )

    assert basic_config_kwargs == {
        "format": "trace_id=%(otelTraceID)s service=%(otelServiceName)s %(message)s",
        "level": logging.INFO,
    }


def test_log_hook_can_add_fields(log_records):
    provider = tracer_provider()
    tracer = provider.get_tracer(__name__)

    def log_hook(span, record):
        record.custom_user_id = "user-1"

    OsloLogInstrumentor().instrument(
        logger_provider=logger_provider("hook-service"),
        log_hook=log_hook,
    )

    with tracer.start_as_current_span("span"):
        emit_record()

    assert log_records[0].custom_user_id == "user-1"


def test_log_level_accepts_integer(monkeypatch):
    basic_config_kwargs = {}

    def basic_config(**kwargs):
        basic_config_kwargs.update(kwargs)

    monkeypatch.setattr(logging, "basicConfig", basic_config)

    OsloLogInstrumentor().instrument(
        logger_provider=logger_provider(),
        set_logging_format=True,
        log_level=logging.DEBUG,
    )

    assert basic_config_kwargs["level"] == logging.DEBUG


def _root_otlp_handlers():
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, LoggingHandler)
    ]


def test_instrument_installs_otlp_handler_and_uninstrument_removes_it():
    instrumentor = OsloLogInstrumentor()
    instrumentor.instrument(logger_provider=logger_provider())

    assert len(_root_otlp_handlers()) == 1

    instrumentor.uninstrument()

    assert not _root_otlp_handlers()


def test_enable_log_auto_instrumentation_false_skips_handler():
    OsloLogInstrumentor().instrument(
        logger_provider=logger_provider(),
        enable_log_auto_instrumentation=False,
    )

    assert not _root_otlp_handlers()


def test_records_are_exported_through_logger_provider():
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(
        resource=Resource.create({SERVICE_NAME: "export-service"})
    )
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

    OsloLogInstrumentor().instrument(logger_provider=provider)

    logger = logging.getLogger("oslo-log-export-test")
    logger.setLevel(logging.INFO)
    logger.info("exported message")

    finished_logs = exporter.get_finished_logs()
    assert len(finished_logs) == 1
    assert finished_logs[0].log_record.body == "exported message"


def _oslo_conf():
    conf = cfg.ConfigOpts()
    oslo_logging.register_options(conf)
    conf([])
    return conf


def test_oslo_setup_guard_reinstalls_handler():
    OsloLogInstrumentor().instrument(logger_provider=logger_provider())
    assert len(_root_otlp_handlers()) == 1

    # oslo.log setup removes every root handler and re-adds its own; the guard
    # must put our exporting handler back.
    oslo_logging.setup(_oslo_conf(), "otel-oslo-test")

    assert len(_root_otlp_handlers()) == 1


def test_uninstrument_restores_oslo_setup():
    original_setup = oslo_logging.setup
    instrumentor = OsloLogInstrumentor()
    instrumentor.instrument(logger_provider=logger_provider())
    assert oslo_logging.setup is not original_setup

    instrumentor.uninstrument()

    assert oslo_logging.setup is original_setup
    # With the guard gone and the handler removed, setup must not re-add it.
    oslo_logging.setup(_oslo_conf(), "otel-oslo-test")
    assert not _root_otlp_handlers()
