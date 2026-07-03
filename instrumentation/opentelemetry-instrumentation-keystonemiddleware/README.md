# OpenTelemetry keystonemiddleware instrumentation

Instruments the [`keystonemiddleware`](https://docs.openstack.org/keystonemiddleware/)
`auth_token` WSGI middleware that OpenStack services mount in front of their
request pipeline.

It wraps `keystonemiddleware.auth_token.BaseAuthProtocol.process_request` -- the
single method through which the stock `AuthProtocol` and any subclass validate
the request's bearer token(s) -- so every authentication produces a span:

- the outcome: whether a user and/or service token was present and accepted;
- for accepted user tokens, the resolved identity (`user.id`, `user.name`,
  `user.roles`) and OpenStack scope (`openstack.project_id`,
  `openstack.project_name`, `openstack.domain_id`).

Because `auth_token` is usually the first middleware to see an inbound request,
it also continues a distributed trace: when no span is already active the
instrumentor extracts the W3C trace context from the incoming headers and opens a
`SERVER` span, so a trace started by an instrumented client (for example the
OpenStack SDK) carries on into the service. When a span is already active the
authentication span is nested under it as `INTERNAL`.

## Installation

```bash
pip install opentelemetry-instrumentation-keystonemiddleware
```

## Usage

```python
from opentelemetry.instrumentation.keystonemiddleware import (
    KeystoneMiddlewareInstrumentor,
)

KeystoneMiddlewareInstrumentor().instrument()
```
