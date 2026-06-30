# OpenTelemetry TaskFlow Instrumentation

OpenStack TaskFlow instrumentation for OpenTelemetry.

It records each engine execution as a root `taskflow.flow.run` span and each
atom `execute`/`revert` call as a child span, so a whole flow shows up as a
single trace:

```
taskflow.flow.run
  taskflow.task.execute
  taskflow.task.revert
  taskflow.retry.execute
  taskflow.retry.revert
```

Instead of patching atom methods, it uses TaskFlow's native notification API: it
attaches an OpenTelemetry [listener](https://docs.openstack.org/taskflow/latest/user/notifications.html)
to every engine's flow and atom notifiers. This observes work wherever it runs
(including the worker threads of the parallel engine) and keeps correct
parent/child linkage regardless of which thread executes an atom.

Spans are annotated with flow/atom names and UUIDs. Atom arguments and results
are never recorded.

## Usage

```python
from opentelemetry.instrumentation.taskflow import TaskflowInstrumentor

TaskflowInstrumentor().instrument()
```

Once instrumented, any engine you create — via `taskflow.engines.run()`,
`taskflow.engines.load()`, a flow factory, or directly — is traced
automatically.
