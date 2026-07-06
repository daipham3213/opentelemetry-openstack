# OpenTelemetry oslo.service Instrumentation

OpenStack oslo.service instrumentation for OpenTelemetry.

`oslo.service` is OpenStack's service framework: it launches services and runs
their background work (`ThreadGroup`, looping/periodic calls, RPC servers) on
either a *threading* or an *eventlet* backend. This instrumentor emits no
telemetry of its own — it makes the active trace context **survive the
concurrency boundaries** oslo.service introduces, so spans and `oslo.log` records
created in spawned work stay correlated with the request that spawned them.

The trace a record/span is correlated to comes from the *current* OpenTelemetry
context, which lives in a `contextvars.Context`. Python does not copy that
context into new threads or greenthreads, so work handed to a worker would
otherwise start on a fresh, disconnected trace (or export `trace_id`/`span_id`
as `0`).

On `instrument()` it:

- selects the oslo.service backend (threading by default) unless the host has
  already chosen one — set `OTEL_PYTHON_OSLO_SERVICE_BACKEND=threading|eventlet`
  or pass `backend="..."`;
- propagates the active context across oslo.service's concurrency primitives.

## What gets propagated

| Backend    | Primitive                                        | Wrapped |
| ---------- | ------------------------------------------------ | ------- |
| threading  | `ThreadGroup.add_thread` (a `threading.Thread` per task) | `threading.Thread.start` / `run` |
| threading  | looping / periodic calls (a `futurist` pool)     | `futurist.ThreadPoolExecutor.submit` |
| eventlet   | greenthreads (`ThreadGroup`, hand-offs)          | `eventlet.spawn` / `spawn_n` / `spawn_after`, `GreenPool.spawn` / `spawn_n`, `futurist.GreenThreadPoolExecutor.submit` |

Not covered:

- stdlib `concurrent.futures` thread pools — enable
  [`opentelemetry-instrumentation-threading`](https://pypi.org/project/opentelemetry-instrumentation-threading/);
- `threading.Timer` / `Thread` subclasses that override `run`, and cross-process
  (`ProcessPoolExecutor`) work.

> **Note:** this context propagation previously lived in the oslo.messaging
> (eventlet spawns) and oslo.log (threads/futurist) instrumentors. It is
> consolidated here so it is defined once and benefits every OpenStack service,
> regardless of which of those instrumentors it also enables.

## Usage

```python
from opentelemetry.instrumentation.oslo_service import OsloServiceInstrumentor

OsloServiceInstrumentor().instrument()
```

To pair with log/trace instrumentation, enable it alongside the others (order
does not matter):

```python
OsloServiceInstrumentor().instrument()
OsloMessagingInstrumentor().instrument(tracer_provider=tracer_provider)
OsloLogInstrumentor().instrument(logger_provider=logger_provider)
```
