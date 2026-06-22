# OpenTelemetry TaskFlow Instrumentation

OpenStack TaskFlow instrumentation for OpenTelemetry.

It records every `execute`/`revert` call on a `taskflow.task.Task` as a span
named `taskflow.task.<method>`, annotated with the task's class, method and
name. Because concrete tasks override `execute`/`revert`, the instrumentor wraps
`Task.__getattribute__` so those overrides are traced wherever they are defined.

## Usage

```python
from opentelemetry.instrumentation.taskflow import TaskflowInstrumentor

TaskflowInstrumentor().instrument()
```
