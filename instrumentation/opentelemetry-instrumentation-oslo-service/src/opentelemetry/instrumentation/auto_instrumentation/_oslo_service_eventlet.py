"""Eventlet monkey-patch, factored out so it can run at the earliest hook."""

import os
import sys
import warnings
from contextlib import contextmanager

__all__ = [
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
    "reset_hub_on_fork",
    "try_patch",
    "wrap_initialize",
]

#: Opt-in flag: when ``"true"`` (case-insensitive), eventlet is monkey-patched.
OTEL_PYTHON_EVENTLET_MONKEY_PATCH = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

#: Backend forced for oslo.service once eventlet has been patched.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"

#: ``os.register_at_fork`` cannot be undone, so the handler goes on once.
_hub_reset_installed = False


def reset_hub_on_fork() -> bool:
    """Give every forked child a fresh eventlet hub.

    A child inherits a copy of the parent's hub, greenlets included. Among them
    are threads still waiting to start -- under eventlet ``Thread.start()`` only
    schedules a hub timer -- and the child's ``threading._after_fork()`` clears
    ``threading._limbo`` out from under them, so each one bootstraps into
    ``KeyError: <Timer(Thread-18, stopped daemon ...)>`` and dies. Any library's
    threads, whichever were mid-start at fork time.

    Dropping the hub drops those timers with it. Keeping them would be worse:
    the parent's in-flight greenlets would run twice, in two processes, over
    shared sockets. oslo.service does the same in
    ``ProcessLauncher._child_process``; this covers every fork, and runs early
    enough to beat the handlers that start threads -- ``after_in_child``
    callbacks fire in registration order.

    :returns: ``True`` if the handler is installed, now or by an earlier call.
    """
    global _hub_reset_installed

    if _hub_reset_installed:
        return True
    if not hasattr(os, "register_at_fork"):  # pragma: no cover - POSIX only
        return False

    def _fresh_hub() -> None:
        # Resolved from sys.modules, not imported: a process that never uses
        # eventlet pays nothing and is not given a hub. Never raises -- this
        # runs inside os.fork(), in a child that may be about to exec.
        try:
            hubs = sys.modules.get("eventlet.hubs")
            if hubs and getattr(hubs._threadlocal, "hub", None) is not None:
                hubs.use_hub()
        except Exception:  # noqa: BLE001
            pass

    os.register_at_fork(after_in_child=_fresh_hub)
    _hub_reset_installed = True
    return True


@contextmanager
def _strip_path():
    """Hide this directory from ``PYTHONPATH`` for the duration.

    Anything spawned while it is visible re-runs auto-instrumentation, which
    spawns again -- so keep it out over the calls that may fork.
    """
    this_dir = os.path.dirname(os.path.abspath(__file__))
    original = os.environ.get("PYTHONPATH")
    entries = original.split(os.pathsep) if original else []

    if this_dir not in entries:
        yield
        return

    os.environ["PYTHONPATH"] = os.pathsep.join(
        entry for entry in entries if entry != this_dir
    )
    try:
        yield
    finally:
        os.environ["PYTHONPATH"] = original


def try_patch() -> None:
    """Monkey-patch eventlet when opted in, and pin the oslo.service backend.

    No-op unless ``OTEL_PYTHON_EVENTLET_MONKEY_PATCH`` is ``"true"``. A missing
    eventlet is surfaced with :func:`warnings.warn`, not the module logger: this
    runs before logging is configured.

    :returns: ``None``.
    """
    flag = os.environ.get(OTEL_PYTHON_EVENTLET_MONKEY_PATCH) or "false"
    if flag.strip().lower() != "true":
        return

    with _strip_path():
        try:
            eventlet = __import__("eventlet")
            eventlet.monkey_patch()
            # Forced, not setdefault: a stale value would re-introduce the
            # fork/socket mismatch the patch prevents.
            os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
            # Earliest point available, so this precedes every other
            # after_in_child callback that starts a thread.
            reset_hub_on_fork()
        except ImportError:
            warnings.warn(
                f"{OTEL_PYTHON_EVENTLET_MONKEY_PATCH} is set to true, but "
                "eventlet is not installed; continuing without "
                "monkey-patching.",
                stacklevel=2,
            )


def wrap_initialize(auto_instrumentation_module) -> None:
    """Fold :func:`try_patch` into ``auto_instrumentation_module._initialize``.

    Keeps the patch applied when ``initialize()`` is called again, re-entrantly
    or by application code.

    :param auto_instrumentation_module: the already-imported
        ``opentelemetry.instrumentation.auto_instrumentation`` module.
    :returns: ``None``.
    """
    original = auto_instrumentation_module._initialize

    def _initialize_with_eventlet_patch(*args, **kwargs):
        try_patch()
        return original(*args, **kwargs)

    auto_instrumentation_module._initialize = _initialize_with_eventlet_patch
