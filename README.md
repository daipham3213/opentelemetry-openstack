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

## Auto-instrumentation image

The repository also publishes a Python auto-instrumentation image,
`daipham3213/otel-autoinstrumentation-openstack`, built from
[`autoinstrumentation/Dockerfile`](autoinstrumentation/Dockerfile). It is a
drop-in replacement for the upstream
[`autoinstrumentation-python`](https://github.com/open-telemetry/opentelemetry-operator#opentelemetry-auto-instrumentation-injection)
image: it bundles `opentelemetry-distro`, the standard contrib
instrumentations, **and** the three OpenStack packages above, so OpenStack
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
