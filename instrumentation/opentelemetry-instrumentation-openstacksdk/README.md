# OpenTelemetry OpenStack SDK Instrumentation

OpenStack SDK (`openstacksdk`) instrumentation for OpenTelemetry.

The OpenStack SDK reaches every service through an
`openstack.connection.Connection` whose service proxies (`conn.compute`,
`conn.network`, ...) subclass `openstack.proxy.Proxy`. Every REST call the SDK
makes funnels through a single method, `Proxy.request`, no matter which
resource method the application called.

This instrumentor wraps `Proxy.request` so each SDK REST call becomes a
`CLIENT` span. On `instrument()` it:

- records one span per request, named `{service_type} {HTTP method}` (for
  example `compute GET`), falling back to the bare HTTP method when the proxy
  has no service type;
- sets the standard HTTP client attributes `http.request.method`,
  `http.response.status_code`, `url.full`, `server.address` and `server.port`
  (the fully resolved URL is read back from the response once keystoneauth has
  looked up the endpoint);
- adds `openstack.service_type` and `openstack.region_name` describing the
  proxy that issued the call;
- injects the active W3C trace context into the outgoing request headers, so a
  trace started in the SDK continues on the OpenStack service that handles the
  request (when that service is instrumented too).

`Proxy.request` defaults to `raise_exc=False`, so HTTP error statuses come back
as ordinary responses rather than exceptions; the span status is set to
`ERROR` for any status code `>= 400`, and transport-level failures are recorded
as exceptions on the span.

> **Note:** the span is created at the proxy layer, so keystoneauth's own token
> and version-discovery requests — which go straight through the session rather
> than a proxy — are not traced. Only genuine SDK service calls produce spans.

## Usage

```python
from opentelemetry.instrumentation.openstacksdk import (
    OpenStackSDKInstrumentor,
)

OpenStackSDKInstrumentor().instrument()

import openstack

conn = openstack.connect(cloud="mycloud")
list(conn.compute.servers())  # emits a "compute GET" client span
```

Pass a `tracer_provider` to `instrument()` to use a provider other than the
global default.
