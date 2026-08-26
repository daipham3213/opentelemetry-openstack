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

Span attributes follow the conventions oslo.messaging's own built-in tracing
established (added upstream in ``oslo_messaging/_tracing``), so a trace reads
the same whichever emitter produced a span: ``messaging.operation`` is the call
style (``call``/``cast``/``send``) or ``receive``, ``messaging.destination.name``
is the target topic, ``rpc.method`` is the bare method with the namespace in
``rpc.service``, and the OpenStack request id is recorded as
``openstack.request_id``. Two things deliberately differ: ``messaging.system``
is derived from the configured ``transport_url`` rather than hardcoded to
``rabbitmq``, and ``rpc.system`` is recorded as ``oslo.messaging``.

.. warning::

   Do not enable this instrumentor *and* oslo.messaging's built-in tracing
   (``[oslo_messaging_tracing] tracing_enabled = true``) at the same time. They
   wrap different layers -- the RPC client/server versus the transport and
   dispatchers -- so both fire and every message gets a duplicate pair of
   producer and consumer spans.

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
