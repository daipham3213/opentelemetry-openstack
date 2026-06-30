"""OpenTelemetry instrumentation for oslo.messaging.

Usage::

    from opentelemetry.instrumentation.oslo_messaging import (
        OsloMessagingInstrumentor,
    )

    OsloMessagingInstrumentor().instrument()

The instrumentor patches two thin layers:

**Producer — context injection.** ``Transport._send`` and
``Transport._send_notification`` inject the active W3C trace context into the
already-serialized context dictionary that goes on the wire. This is
serializer-agnostic (it runs *after* the serializer) and covers every send
path: RPC calls, casts, fanout, replies, and notifications. No producer span is
created — the linkage rides on whatever span is active in the caller.

**Consumer — spans.** ``RPCDispatcher.dispatch`` and
``NotificationDispatcher.dispatch`` open ``CONSUMER`` spans parented to the
producer, extracting context from the incoming message:

* ``RPCDispatcher.dispatch`` → ``oslo.messaging.rpc.process``
* ``NotificationDispatcher.dispatch`` → ``oslo.messaging.notification.process``

Message payloads are never recorded as span attributes.
"""

import logging
from importlib import import_module
from os import environ

from oslo_service import backend

_LOG = logging.getLogger(__name__)

_OSLO_SERVICE_BACKEND_MAPPING = {
    "threading": backend.BackendType.THREADING,
    "eventlet": backend.BackendType.EVENTLET,
}

OSLO_SERVICE_BACKEND = environ.get(
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND", "threading"
)

OSLO_SERVICE_BACKEND_TYPE = _OSLO_SERVICE_BACKEND_MAPPING[OSLO_SERVICE_BACKEND]

# Only select the backend if the host application has not already chosen one.
# ``init_backend`` raises if a *different* backend is already active, and a
# backend gets selected as a side effect of importing parts of oslo.messaging
# (it defaults to eventlet), so blindly initializing here would break any host
# that imported oslo.messaging first. Respect the existing choice instead.
_current_backend = backend.get_backend_type()
if _current_backend is None:
    backend.init_backend(OSLO_SERVICE_BACKEND_TYPE)
elif _current_backend != OSLO_SERVICE_BACKEND_TYPE:
    _LOG.debug(
        "oslo_service backend already set to %r; leaving it unchanged "
        "(requested %r via OTEL_PYTHON_OSLO_SERVICE_BACKEND)",
        _current_backend.value,
        OSLO_SERVICE_BACKEND_TYPE.value,
    )

instrument = import_module(
    "opentelemetry.instrumentation.oslo_messaging.instrument"
)
OsloMessagingInstrumentor = instrument.OsloMessagingInstrumentor

__all__ = ["OsloMessagingInstrumentor"]
