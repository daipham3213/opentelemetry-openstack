# OpenTelemetry oslo.log Instrumentation

OpenStack oslo.log instrumentation for OpenTelemetry.

`oslo.log` builds on top of the standard library `logging` module, so this
instrumentor extends `opentelemetry-instrumentation-logging`. On `instrument()`
it:

- injects the OpenTelemetry attributes `otelTraceID`, `otelSpanID`,
  `otelTraceSampled` and `otelServiceName` onto every log record, so they are
  available to oslo.log's `ContextFormatter` format strings;
- resolves the service name from the (optional) `logger_provider` so it matches
  the resource the logs are exported with;
- installs a `LoggingHandler` on the root logger so oslo.log records are
  exported through the OpenTelemetry logs pipeline (disable with
  `enable_log_auto_instrumentation=False`).

## Usage

```python
from opentelemetry.instrumentation.oslo_log import OsloLogInstrumentor

OsloLogInstrumentor().instrument(logger_provider=logger_provider)
```

To surface the injected trace context in oslo.log output, reference the fields
from `logging_context_format_string` / `logging_default_format_string` in your
oslo.config:

```ini
[DEFAULT]
logging_default_format_string = %(asctime)s %(levelname)s %(name)s [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(message)s
logging_context_format_string = %(asctime)s %(levelname)s %(name)s [%(request_id)s trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(message)s
```
