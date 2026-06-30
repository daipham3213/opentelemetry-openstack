"""Example: enabling oslo.messaging instrumentation in a service.

Run with a console exporter so produced spans are printed::

    python examples/usage.py

In a real service you would configure an OTLP exporter instead and call
``OsloMessagingInstrumentor().instrument()`` once, early in start-up — before
any transport, RPC client/server, or notifier is created.
"""

from opentelemetry import propagate, trace
from opentelemetry.instrumentation.oslo_messaging import (
    OsloMessagingInstrumentor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)


def main() -> None:
    """Instrument oslo.messaging and demonstrate context propagation."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

    # One call, early in start-up. Idempotent: a second call is a no-op.
    OsloMessagingInstrumentor().instrument(tracer_provider=provider)

    # From here on:
    #   * Transport._send / _send_notification inject W3C trace context into the
    #     on-the-wire context dict (no broker needed to see this below).
    #   * RPCDispatcher / NotificationDispatcher.dispatch open CONSUMER spans
    #     parented to the producer.
    #
    # ``propagate.inject`` below mirrors exactly what the instrumented
    # ``Transport._send`` does to the message's context dictionary.
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("producer-work"):
        wire_ctxt = {"user_id": "alice"}
        propagate.inject(wire_ctxt)

    print("context placed on the wire:", wire_ctxt)
    assert "traceparent" in wire_ctxt

    # To stop instrumenting (e.g. in tests):
    OsloMessagingInstrumentor().uninstrument()


if __name__ == "__main__":
    main()
