"""Carry the active OpenTelemetry context across oslo.service's concurrency.

Trace correlation -- of spans *and* of oslo.log records, which read the current
span from the OpenTelemetry context -- breaks whenever work crosses a
concurrency boundary, because the context lives in a ``contextvars.Context`` that
is not copied into the new thread or greenthread. The spawned work then starts
with an empty context and lands on a fresh, disconnected trace (or exports
``trace_id``/``span_id`` == 0).

oslo.service hands request work off across exactly these boundaries, and which
one depends on the active backend:

* **threading backend** -- ``ThreadGroup.add_thread`` spawns a fresh
  ``threading.Thread`` per task, and looping/periodic calls run their body on a
  ``futurist`` thread pool;
* **eventlet backend** -- work is handed to greenthreads via ``eventlet.spawn``
  / ``spawn_n`` / ``spawn_after`` and ``GreenPool.spawn`` / ``spawn_n`` (e.g.
  nova-compute's ``build_and_run_instance`` right after an RPC is dispatched).

This module wraps every one of those entry points so the context active on the
dispatching thread/greenthread is re-attached around the spawned work. Two
shapes of primitive need two shapes of wrapper:

* **callable-passing spawns** (``eventlet`` spawns, ``futurist`` pool
  ``submit``) -- capture the context at call time and wrap the passed callable;
* **object-based threads** (``threading.Thread``) -- capture the context at
  ``start`` (on the thread that owns the span) and re-attach it around ``run``.
  A per-task ``Thread`` is correct here; pooled workers reuse the same
  ``Thread`` across tasks, so those are handled at ``submit`` instead.

Not handled: stdlib ``concurrent.futures`` pools (use
``opentelemetry-instrumentation-threading``), ``threading.Timer`` / ``Thread``
subclasses that override ``run``, and cross-process work.

Every wrapper is a ``wrapt``-style ``(wrapped, instance, args, kwargs)`` and
never raises on its own account: context propagation must not break the host
application, so the wrapped callable is always invoked and any attached context
is always detached.
"""

import functools
import threading
from typing import Any, Callable, List, Tuple

import wrapt

from opentelemetry import context
from opentelemetry.instrumentation.utils import (
    is_instrumentation_enabled,
    unwrap,
)

# Thread instance attribute holding the context captured at ``start`` time.
# Namespaced so it never clashes with opentelemetry-instrumentation-threading's
# ``_otel_context`` if both patch ``threading.Thread``.
_OTEL_CONTEXT_ATTR = "_otel_oslo_service_context"

WrappedFn = Callable[..., Any]
Wrapper = Callable[[WrappedFn, Any, tuple, dict], Any]
#: ``(owner, method_name, callable_index)`` for a callable-passing spawn.
SpawnTarget = Tuple[Any, str, int]
#: ``(owner, method_name)`` recorded so uninstrument can restore it.
WrapTarget = Tuple[Any, str]

# eventlet greenthread spawns to patch, as ``(owner, method, callable_index)``.
# ``callable_index`` is where the spawned function sits in the positional args
# (``spawn_after`` takes a delay first).
_EVENTLET_SPAWNS: Tuple[SpawnTarget, ...] = ()
try:
    import eventlet
    from eventlet.greenpool import GreenPool

    _EVENTLET_SPAWNS = (
        (eventlet, "spawn", 0),
        (eventlet, "spawn_n", 0),
        (eventlet, "spawn_after", 1),
        (GreenPool, "spawn", 0),
        (GreenPool, "spawn_n", 0),
    )
except ImportError:
    eventlet = None
    GreenPool = None

# futurist pool executors whose ``submit`` should carry the caller's context to
# the task. ``SynchronousExecutor`` runs inline (context already correct) and
# ``ProcessPoolExecutor`` cannot share contextvars, so both are left out.
_FUTURIST_SUBMITS: Tuple[SpawnTarget, ...] = ()
try:
    import futurist

    _FUTURIST_SUBMITS = tuple(
        (executor, "submit", 0)
        for executor in (
            getattr(futurist, "ThreadPoolExecutor", None),
            getattr(futurist, "GreenThreadPoolExecutor", None),
        )
        if executor is not None
    )
except ImportError:
    futurist = None


def spawn_wrapper(func_index: int = 0) -> Wrapper:
    """Wrap a spawn/submit so its callable runs under the caller's context.

    Captures the context at call time (on the thread/greenthread that owns the
    span) and re-attaches it around the callable when the spawned work runs, so
    the work continues the same trace.

    :param func_index: Position of the spawned callable among the positional
        arguments -- ``0`` for ``spawn``/``spawn_n``/``GreenPool.spawn`` and a
        pool's ``submit``; ``1`` for ``spawn_after``, whose first argument is the
        delay.
    :returns: A ``wrapt``-style wrapper ``(wrapped, instance, args, kwargs)``.
    """

    def wrapper(
        wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if not is_instrumentation_enabled() or len(args) <= func_index:
            return wrapped(*args, **kwargs)

        func = args[func_index]
        if not callable(func):
            return wrapped(*args, **kwargs)

        captured = context.get_current()

        @functools.wraps(func)
        def traced(*call_args: Any, **call_kwargs: Any) -> Any:
            token = context.attach(captured)
            try:
                return func(*call_args, **call_kwargs)
            finally:
                context.detach(token)

        args = args[:func_index] + (traced,) + args[func_index + 1 :]
        return wrapped(*args, **kwargs)

    return wrapper


def _wrap_thread_start(
    wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
) -> Any:
    """Stash the current context on the thread before it starts.

    Runs on the thread that owns the span (the one calling ``start``), so
    ``get_current`` here is the context the spawned work should continue.
    """
    setattr(instance, _OTEL_CONTEXT_ATTR, context.get_current())
    return wrapped(*args, **kwargs)


def _wrap_thread_run(
    wrapped: WrappedFn, instance: Any, args: tuple, kwargs: dict
) -> Any:
    """Re-attach the captured context while the thread body runs."""
    captured = getattr(instance, _OTEL_CONTEXT_ATTR, None)
    if captured is None:
        return wrapped(*args, **kwargs)
    token = context.attach(captured)
    try:
        return wrapped(*args, **kwargs)
    finally:
        context.detach(token)


def install() -> List[WrapTarget]:
    """Wrap the thread/greenthread/executor entry points.

    :returns: the wrapped targets, passed back to :func:`uninstall` so only what
        this module wrapped is undone.
    """
    wrapped: List[WrapTarget] = []

    # Native threads: bare threading.Thread and oslo_service's threading-backend
    # ThreadGroup.add_thread (a fresh Thread per task).
    for owner, name, thread_wrapper in (
        (threading.Thread, "start", _wrap_thread_start),
        (threading.Thread, "run", _wrap_thread_run),
    ):
        wrapt.wrap_function_wrapper(owner, name, thread_wrapper)
        wrapped.append((owner, name))

    # Callable-passing spawns: eventlet greenthreads and futurist pools (the
    # latter used by oslo_service's looping/periodic calls).
    for owner, name, func_index in (*_EVENTLET_SPAWNS, *_FUTURIST_SUBMITS):
        wrapt.wrap_function_wrapper(owner, name, spawn_wrapper(func_index))
        wrapped.append((owner, name))

    return wrapped


def uninstall(wrapped: List[WrapTarget]) -> None:
    """Undo the wrapping installed by :func:`install`."""
    for owner, name in wrapped:
        unwrap(owner, name)
