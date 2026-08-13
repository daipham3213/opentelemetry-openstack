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

## Auto-instrumentation

Under `opentelemetry-instrument`, this package also ships an OpenTelemetry
distro and configurator so the instrumentor is enabled without any code change.

### Eventlet monkey-patching

Services that run on the eventlet backend must monkey-patch the stdlib
*before* anything imports `socket`, `threading`, etc. — otherwise the patch is
partial and context propagation across greenthreads is unreliable. The
configurator can do this for you, at the earliest point in the
auto-instrumentation startup, when you opt in:

```bash
export OTEL_PYTHON_CONFIGURATOR=oslo_service
export OTEL_PYTHON_EVENTLET_MONKEY_PATCH=true
opentelemetry-instrument <your-service>
```

- `OTEL_PYTHON_EVENTLET_MONKEY_PATCH=true` tells the configurator to call
  `eventlet.monkey_patch()`. (`eventlet` must be installed; if it is not, a
  warning is emitted and startup continues unpatched.)
- `OTEL_PYTHON_CONFIGURATOR=oslo_service` is **required**. Auto-instrumentation
  loads only one configurator, and the stock `opentelemetry-distro` one is
  otherwise selected first — so without this the oslo.service configurator (and
  the monkey-patch) is silently skipped.

If you don't run on eventlet, neither variable is needed.
