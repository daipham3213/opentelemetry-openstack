# OpenTelemetry oslo.log Instrumentation

OpenStack oslo.log instrumentation for OpenTelemetry.

`oslo.log` builds on top of the standard library `logging` module. This
instrumentor exports OpenStack service logs through the OpenTelemetry logs
pipeline (and onward to an OTLP collector) while keeping them correlated with
traces. On `instrument()` it:

- injects the OpenTelemetry attributes `otelTraceID`, `otelSpanID`,
  `otelTraceSampled` and `otelServiceName` onto every log record, so they are
  available to oslo.log's `ContextFormatter` format strings;
- installs an `OsloLogHandler` on oslo.log's root logger so records are
  exported through the configured `logger_provider` (disable with
  `enable_log_auto_instrumentation=False`);
- maps oslo request context (request id, user/project ids, ...) onto exported
  record attributes (disable with `map_oslo_context=False`).

The exported log body is the raw log message; structured fields travel as
attributes.

`oslo_log.log.setup` rebuilds the root logger's handlers from oslo.config,
dropping every existing handler. The instrumentor wraps `setup` so the
exporting handler is re-attached afterwards, and keeps working even when
`setup` runs (or runs again) after `instrument()`.

> **Note:** the handler is attached to oslo.log's root logger, not the stdlib
> root logger. Records from non-oslo libraries that log straight to the stdlib
> root are handled by `opentelemetry-instrumentation-logging`, not this package.

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

## Exported context attributes

When `map_oslo_context` is enabled (the default), the following oslo request
context fields are mapped onto exported log record attributes:

| oslo context value | exported attribute             |
| ------------------ | ------------------------------ |
| `request_id`       | `openstack.request_id`         |
| `global_request_id`| `openstack.global_request_id`  |
| `user`             | `openstack.user_id`            |
| `user_name`        | `openstack.user_name`          |
| `project`          | `openstack.project_id`         |
| `project_name`     | `openstack.project_name`       |
| `domain`           | `openstack.domain_id`          |
| `user_domain`      | `openstack.user_domain_id`     |
| `project_domain`   | `openstack.project_domain_id`  |
| `roles`            | `openstack.roles`              |
| `resource_uuid`    | `openstack.resource_uuid`      |

Authentication tokens are never exported.
