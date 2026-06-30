# OpenTelemetry oslo.messaging Instrumentation

OpenStack oslo.messaging instrumentation for OpenTelemetry.

It patches two thin layers to produce end-to-end producer → consumer traces.

**Producer — context injection.** `Transport._send` and
`Transport._send_notification` inject the current W3C trace context into the
message's on-the-wire context dictionary:

- This runs *after* the serializer, so it is serializer-agnostic — it works with
  any `Serializer` (including production's `RequestContextSerializer`, whose
  `to_dict()` would otherwise drop unknown keys).
- It is a single chokepoint covering every send path: RPC calls, casts, fanout,
  replies, and notifications.
- No producer span is created; the trace linkage rides on whatever span is
  already active in the caller (e.g. the WSGI request span).

**Consumer — spans.** The dispatchers open `CONSUMER` spans parented to the
producer, extracting context from the incoming message:

- `RPCDispatcher.dispatch` → a `CONSUMER` span `oslo.messaging.rpc.process`
- `NotificationDispatcher.dispatch` → a `CONSUMER` span
  `oslo.messaging.notification.process`

The span is scoped to the dispatch call, so the remote context is never attached
beyond message handling. Message payloads are never recorded as span attributes.
The batch notification dispatcher is not instrumented.

## Usage

```python
from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
)

OsloMessagingInstrumentor().instrument()
```
