"""OpenTelemetry trace listener for TaskFlow engines.

TaskFlow exposes a first-class observation API: every engine owns a flow-level
:class:`~taskflow.types.notifier.Notifier` (``engine.notifier``) and an
atom-level one (``engine.atom_notifier``), and
:class:`taskflow.listeners.base.Listener` is the supported way to subscribe to
both. This module rides that API instead of monkey-patching atom methods, which
means it observes work wherever it runs -- including the worker threads of
:class:`~taskflow.engines.action_engine.engine.ParallelActionEngine` -- and it
keeps proper parent/child linkage because every atom span is created with the
flow span's context explicitly, not by relying on the ambient context of the
thread that happens to fire the callback.

Span shape::

    taskflow.flow.run               (one per engine execution)
      taskflow.task.execute         (per task execute() call)
      taskflow.task.revert          (per task revert() call)
      taskflow.retry.execute        (per retry controller execute() call)
      taskflow.retry.revert         (per retry controller revert() call)

Atom and flow *names*/UUIDs are recorded as attributes; results and arguments
are never recorded.
"""

import logging
import threading
from typing import Dict, Optional, Tuple

from taskflow import states
from taskflow.engines import base as engines
from taskflow.listeners import base as listeners
from taskflow.types import failure as ft

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_LOG = logging.getLogger(__name__)

#: Atom states that open a traced phase, mapped to the method they represent.
_ATOM_START_METHODS = {
    states.RUNNING: "execute",
    states.REVERTING: "revert",
}

#: Atom states that close a traced phase, mapped to ``(method, is_failure)``.
_ATOM_FINISH_METHODS = {
    states.SUCCESS: ("execute", False),
    states.FAILURE: ("execute", True),
    states.REVERTED: ("revert", False),
    states.REVERT_FAILURE: ("revert", True),
}

#: Flow states that terminate the flow span, mapped to ``is_failure``.
_FLOW_FINISH_STATES = {
    states.SUCCESS: False,
    states.FAILURE: True,
    states.REVERTED: True,
    states.REVERT_FAILURE: True,
    states.SUSPENDED: False,
}


def _set_failure(span: Span, result: object) -> None:
    """Mark ``span`` as failed using a TaskFlow ``Failure`` result.

    The result attached to a failing atom is a
    :class:`taskflow.types.failure.Failure`. Its underlying exception may have
    been lost during serialization (e.g. results returned from a worker-based
    engine), in which case only the stringified form is available.

    :param span: The span to annotate.
    :param result: The ``result`` entry from the notification details.
    """
    description = None
    if isinstance(result, ft.Failure):
        exception = result.exception
        if exception is not None:
            span.record_exception(exception)
        description = result.exception_str
    span.set_status(Status(StatusCode.ERROR, description))


class _TraceListener(listeners.Listener):
    """A TaskFlow listener that turns engine notifications into spans.

    One instance is attached per engine. It listens for every flow and atom
    state transition and maintains the open spans for the in-flight flow and
    atoms.

    :param engine: The engine to observe.
    :param tracer: The tracer used to create spans.
    """

    def __init__(self, engine: engines.Engine, tracer: trace.Tracer):
        super().__init__(engine)
        self._tracer = tracer
        self._lock = threading.Lock()
        self._flow_span: Optional[Span] = None
        self._flow_context: Optional[context_api.Context] = None
        # Keyed by (atom_uuid, method) so an atom that is executed *and* later
        # reverted -- or retried -- never collides with itself.
        self._atom_spans: Dict[Tuple[str, str], Span] = {}

    # -- flow ---------------------------------------------------------------

    def _flow_receiver(self, state, details):
        try:
            if state == states.RUNNING:
                self._start_flow(details)
            elif state in _FLOW_FINISH_STATES:
                self._finish_flow(state)
        except Exception:  # never let tracing break the flow
            _LOG.warning(
                "Error handling TaskFlow flow notification (state=%s)",
                state,
                exc_info=True,
            )

    def _start_flow(self, details):
        with self._lock:
            if self._flow_span is not None:
                return  # already tracing this flow (e.g. a resume)
            span = self._tracer.start_span(
                "taskflow.flow.run",
                attributes={
                    "taskflow.flow.name": details.get("flow_name"),
                    "taskflow.flow.uuid": details.get("flow_uuid"),
                },
            )
            self._flow_span = span
            self._flow_context = trace.set_span_in_context(span)

    def _finish_flow(self, state):
        with self._lock:
            span = self._flow_span
            self._flow_span = None
            self._flow_context = None
        if span is None:
            return
        if _FLOW_FINISH_STATES.get(state):
            span.set_status(Status(StatusCode.ERROR, state))
        span.end()

    # -- atoms --------------------------------------------------------------

    def _task_receiver(self, state, details):
        self._on_atom("task", state, details)

    def _retry_receiver(self, state, details):
        self._on_atom("retry", state, details)

    def _on_atom(self, kind, state, details):
        try:
            name = details.get(f"{kind}_name")
            uuid = details.get(f"{kind}_uuid")
            if state in _ATOM_START_METHODS:
                self._start_atom(kind, name, uuid, state)
            elif state in _ATOM_FINISH_METHODS:
                self._finish_atom(kind, uuid, state, details)
        except Exception:  # never let tracing break the atom
            _LOG.warning(
                "Error handling TaskFlow %s notification (state=%s)",
                kind,
                state,
                exc_info=True,
            )

    def _start_atom(self, kind, name, uuid, state):
        method = _ATOM_START_METHODS[state]
        with self._lock:
            parent = self._flow_context
            key = (uuid, method)
            if key in self._atom_spans:
                return  # defensive: already open
            span = self._tracer.start_span(
                f"taskflow.{kind}.{method}",
                context=parent,
                attributes={
                    f"taskflow.{kind}.name": name,
                    f"taskflow.{kind}.uuid": uuid,
                    f"taskflow.{kind}.method": method,
                },
            )
            self._atom_spans[key] = span

    def _finish_atom(self, kind, uuid, state, details):
        method, is_failure = _ATOM_FINISH_METHODS[state]
        with self._lock:
            span = self._atom_spans.pop((uuid, method), None)
        if span is None:
            return
        if is_failure:
            _set_failure(span, details.get("result"))
        span.end()


def attach(engine: engines.Engine, tracer: trace.Tracer) -> _TraceListener:
    """Attach a trace listener to ``engine`` and start listening.

    The listener registers itself on the engine's notifiers; those
    registrations hold the only strong reference needed, so the listener lives
    exactly as long as the engine does.

    :param engine: A TaskFlow engine instance to instrument.
    :param tracer: The tracer used to create spans.
    :returns: The registered listener.
    """
    listener = _TraceListener(engine, tracer)
    listener.register()
    return listener
