"""OpenTelemetry instrumentation for oslo.messaging.

Usage::

    from opentelemetry.instrumentation.oslo_messaging import (
        OsloMessagingInstrumentor,
    )

    OsloMessagingInstrumentor().instrument()

The instrumentor patches two thin layers:

**Producer — spans + context injection.** ``Transport._send`` and
``Transport._send_notification`` open a ``PRODUCER`` span named after the
operation being sent -- the RPC method (``start_instance send``) or the
notification event type (``compute.instance.create.start send``) -- and inject
the active W3C trace context into the already-serialized context dictionary that
goes on the wire. This is serializer-agnostic (it runs *after* the serializer)
and covers every send path: RPC calls, casts, fanout, replies, and
notifications.

**Consumer — spans.** ``RPCDispatcher.dispatch`` and
``NotificationDispatcher.dispatch`` open ``CONSUMER`` spans parented to the
producer, extracting context from the incoming message:

* ``RPCDispatcher.dispatch`` → ``oslo.messaging.rpc.process``
* ``NotificationDispatcher.dispatch`` → ``oslo.messaging.notification.process``

Message payloads are never recorded as span attributes.

Spans on both sides carry ``messaging.system`` set to the broker behind the
configured ``transport_url`` (``rabbit://`` -> ``rabbitmq``, ``kafka://`` ->
``kafka``), while ``rpc.system`` remains ``oslo.messaging``.

**Trace continuity across concurrency.** Keeping a consumer's trace on work it
hands to a greenthread or worker thread (as nova-compute's
``build_and_run_instance`` does right after dispatch) is the job of
``opentelemetry-instrumentation-oslo-service``, which also selects the
oslo.service backend. Enable it alongside this instrumentor.
"""

from importlib import import_module

instrument = import_module(
    "opentelemetry.instrumentation.oslo_messaging.instrument"
)
OsloMessagingInstrumentor = instrument.OsloMessagingInstrumentor

__all__ = ["OsloMessagingInstrumentor"]
