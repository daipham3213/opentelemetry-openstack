# OpenTelemetry oslo.messaging Instrumentation

OpenStack oslo.messaging instrumentation for OpenTelemetry.

It patches the client-side entry points so outgoing messages are traced and the
active trace context travels with the message:

- `RPCClient.call` → a `CLIENT` span `oslo.messaging.rpc.call`
- `RPCClient.cast` → a `PRODUCER` span `oslo.messaging.rpc.cast`
- `Notifier._notify` → a `PRODUCER` span `oslo.messaging.notification.publish`

For each traced call the current context is injected into the request context's
`_otel_context` carrier so a consumer instrumented the same way can continue the
trace. Message payloads are never recorded as span attributes.

## Usage

```python
from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
)

OsloMessagingInstrumentor().instrument()
```
