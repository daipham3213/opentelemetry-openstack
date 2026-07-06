# OpenTelemetry OpenStack

A monorepo for OpenTelemetry instrumentation packages targeting OpenStack-related Python libraries.

## Packages

- `opentelemetry-instrumentation-taskflow` — traces TaskFlow engine runs.
  Records a root `taskflow.flow.run` span per engine execution and child spans
  for every task/retry `execute`/`revert`, using TaskFlow's native listener API.
- `opentelemetry-instrumentation-oslo-log` — enriches `oslo.log` records with
  the active trace context.
- `opentelemetry-instrumentation-oslo-messaging` — propagates trace context
  across `oslo.messaging` RPC and notifications and records consumer spans.
- `opentelemetry-instrumentation-oslo-service` — keeps the active trace alive
  across `oslo.service`'s concurrency primitives (native threads, `futurist`
  pools, and eventlet greenthreads), so spans and logs created in spawned work
  stay on the request's trace. Also selects the `oslo.service` backend.
- `opentelemetry-instrumentation-openstacksdk` — records a `CLIENT` span for
  every OpenStack SDK REST call (wrapping `openstack.proxy.Proxy.request`) and
  injects trace context into the outgoing request headers.
- `opentelemetry-instrumentation-keystoneauth1` — records a `CLIENT` span for
  every HTTP call the shared `keystoneauth1` session makes (service calls, token
  fetches, discovery), injecting trace context into the outgoing headers. Sits
  below the SDK proxy, so it also captures the token/discovery calls the SDK
  instrumentation leaves untraced.
- `opentelemetry-instrumentation-keystonemiddleware` — the server-side entry
  point. Wraps the `auth_token` WSGI middleware to open a `SERVER` span for the
  whole request (continuing the trace from the incoming headers) with a nested
  `INTERNAL` span for token validation and the resolved identity.

## How the packages compose into one trace

The packages are designed to hand a single trace off to one another end to end.
Every seam uses the **global** propagator (W3C `traceparent` by default), so
injection and extraction always agree:

```
client process                          service process (e.g. nova-api)
──────────────                          ───────────────────────────────
openstacksdk   Proxy.request  CLIENT
  keystoneauth1 Session.request CLIENT ──HTTP traceparent──▶ keystonemiddleware
                                                             __call__      SERVER
                                                               authenticate  INTERNAL
                                                               handler work…
                                                                 oslo.messaging
                                                                 _send      PRODUCER
                                                                   │ ctxt traceparent
                                                                   ▼
                                                             another service
                                                             RPCDispatcher CONSUMER
```

- **Client → HTTP → server**: the SDK proxy and keystoneauth1 client spans nest
  in one trace and inject the innermost context onto the wire; keystonemiddleware
  extracts it and continues the trace on the server.
- **Server request scope**: keystonemiddleware's `SERVER` span stays active for
  the entire WSGI request, so everything the service handler does while serving
  it — `oslo.messaging` sends, `taskflow` runs, and `oslo.log` records (which
  carry the active `otelTraceID`) — joins the same trace instead of starting a
  disconnected one.
- **RPC / notifications**: `oslo.messaging` injects context into the on-the-wire
  message and the consumer opens a span parented to the producer.
- **Nesting with standard instrumentors**: if an upstream WSGI/framework
  instrumentor already opened the server span, keystonemiddleware nests under it
  as `INTERNAL` rather than opening a second server span.

## Auto-instrumentation image

The repository also publishes a Python auto-instrumentation image,
`daipham3213/otel-autoinstrumentation-openstack`, built from
[`autoinstrumentation/Dockerfile`](autoinstrumentation/Dockerfile). It is a
drop-in replacement for the upstream
[`autoinstrumentation-python`](https://github.com/open-telemetry/opentelemetry-operator#opentelemetry-auto-instrumentation-injection)
image: it bundles `opentelemetry-distro`, the standard contrib
instrumentations, **and** the six OpenStack packages above, so OpenStack
services get traced without changing the application image.

Tags follow the release version (`{{version}}`, `{{major}}.{{minor}}`,
`sha-<commit>`, and `latest` on a published release).

### Use it with the OpenTelemetry Operator

Point the `Instrumentation` resource at this image instead of the default, then
annotate the workloads you want instrumented:

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: openstack
spec:
  exporter:
    endpoint: http://otel-collector:4318
  python:
    image: daipham3213/otel-autoinstrumentation-openstack:latest
```

```yaml
# On the pod/deployment template:
metadata:
  annotations:
    instrumentation.opentelemetry.io/inject-python: "true"
```

The operator injects an init container that copies the bundled packages into
the application container and sets `PYTHONPATH`, so the OpenStack
instrumentations load automatically at startup — no code or image changes
required.

## Tooling

This repository uses:

- `uv` for workspace and dependency management
- `hatchling` for package builds
- `pytest` for tests
- `ruff` for linting and formatting
- `prek` for pre-commit hook execution
- `tox` with `tox-uv` for repeatable test/lint environments

## Common commands

```bash
uv sync --all-packages
```

Run a single package's tests:

```bash
tox -e taskflow
tox -e oslo.log
tox -e oslo.messaging
tox -e openstacksdk
tox -e keystonemiddleware
tox -e keystoneauth1
```

Run every package's tests:

```bash
tox -e all
```

Lint, format, and pre-commit hooks:

```bash
tox -e ruff
tox -e ruff-format
tox -e prek
```
