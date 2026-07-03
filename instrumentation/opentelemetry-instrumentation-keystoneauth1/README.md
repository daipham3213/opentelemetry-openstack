# OpenTelemetry keystoneauth1 instrumentation

Instruments [`keystoneauth1`](https://docs.openstack.org/keystoneauth/), the
shared authentication and HTTP library underneath virtually every OpenStack
client.

It wraps `keystoneauth1.session.Session.request` -- the single method through
which *every* HTTP call the library makes funnels (service API calls, token
requests to Keystone, and version-discovery probes) -- so each call becomes a
`CLIENT` span carrying:

- `http.request.method` and `http.response.status_code`;
- the resolved `url.full` plus `server.address` / `server.port`;
- `openstack.service_type` when the caller supplied an `endpoint_filter`;
- the OpenStack global request id as a correlation id, when present.

The active W3C trace context is injected into the outgoing request headers, so a
trace started in the client continues on the OpenStack service that handles the
request (when that service is itself instrumented, e.g. via
[`opentelemetry-instrumentation-keystonemiddleware`](../opentelemetry-instrumentation-keystonemiddleware)).

Because it sits at the session layer -- below the OpenStack SDK's proxy -- this
instrumentor also captures the token and discovery requests that
[`opentelemetry-instrumentation-openstacksdk`](../opentelemetry-instrumentation-openstacksdk)
deliberately leaves untraced. When both are enabled the session spans nest under
the SDK's proxy spans.

## Installation

```bash
pip install opentelemetry-instrumentation-keystoneauth1
```

## Usage

```python
from opentelemetry.instrumentation.keystoneauth1 import (
    KeystoneAuth1Instrumentor,
)

KeystoneAuth1Instrumentor().instrument()
```
