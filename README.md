# OpenTelemetry OpenStack

A monorepo for OpenTelemetry instrumentation packages targeting OpenStack-related Python libraries.

## Packages

- `opentelemetry-instrumentation-taskflow`
- `opentelemetry-instrumentation-oslo-log`
- `opentelemetry-instrumentation-oslo-messaging`

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

```bash
tox -e taskflow
```

```bash
tox -e ruff
```

```bash
tox -e ruff-format
```

```bash
tox -e prek
```
