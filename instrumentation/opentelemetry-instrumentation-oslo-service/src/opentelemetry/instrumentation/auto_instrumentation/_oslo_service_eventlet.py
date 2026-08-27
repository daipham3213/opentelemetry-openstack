"""Eventlet monkey-patch, factored out so it can run at the earliest hook."""

import os
import sys
import types
import warnings
from contextlib import contextmanager

__all__ = [
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
    "register_forkhook",
    "try_patch",
    "unpatch_sdk",
]

#: Opt-in flag: when ``"true"`` (case-insensitive), eventlet is monkey-patched.
OTEL_PYTHON_EVENTLET_MONKEY_PATCH = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

#: Backend forced for oslo.service once eventlet has been patched.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"

#: ``os.register_at_fork`` cannot be undone, so the handler goes on once.
_forkhook_installed = False

#: Names ``opentelemetry.sdk.metrics._internal.export`` imports from
#: :mod:`threading`; ``_shared_internal`` reaches them through the module.
_SDK_THREAD_NAMES = ("Event", "Lock", "RLock", "Thread")


def register_forkhook() -> bool:
    """Give every forked child a fresh eventlet hub.

    A child inherits a copy of the parent's hub, greenlets included, and among
    them are threads still waiting to start: under eventlet ``Thread.start()``
    only schedules a hub timer. The child's ``threading._after_fork()`` then
    clears ``threading._limbo`` out from under them, so each one bootstraps into
    ``KeyError: <Timer(Thread-18, stopped daemon ...)>`` and dies. It hits
    whichever threads were mid-start, in any library.

    Dropping the hub drops those timers with it. Keeping them would be worse:
    the parent's in-flight greenlets would run twice, in two processes, over
    shared sockets.

    :returns: ``True`` if the handler is installed, now or by an earlier call.
    """
    global _forkhook_installed

    if _forkhook_installed:
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
    _forkhook_installed = True
    return True


def _unpatched_threading():
    """The real :mod:`threading`, or ``None`` if eventlet cannot supply it.

    A mocked eventlet returns a ``Mock``; writing that into the SDK would break
    every exporter, so anything that is not a module is refused.
    """
    try:
        from eventlet import patcher  # noqa: PLC0415

        real = patcher.original("threading")
    except Exception:  # noqa: BLE001 - no eventlet, nothing to keep apart
        return None
    return real if isinstance(real, types.ModuleType) else None


def unpatch_sdk() -> bool:
    """Keep the SDK's exporter threads off eventlet's primitives.

    ``BatchProcessor`` (behind the batch span and log processors) and
    ``PeriodicExportingMetricReader`` each own a worker thread that every other
    thread signals through a shared ``Event``. Green, that raises
    ``greenlet.error: cannot switch to a different thread`` as soon as the
    signalling thread is not the worker's own: ``Event.set()`` schedules on the
    caller's hub, which cannot switch to a greenlet from another thread. Under
    mod_wsgi that is every request, since Apache dispatches each one on a real
    thread of its own.

    Rebinding these modules to the unpatched :mod:`threading` keeps the worker
    and everything signalling it in the real-thread world, where no hub is
    involved.

    :returns: ``True`` if at least one module was rebound.
    """
    real = _unpatched_threading()
    if real is None:
        return False

    rebindings = {
        # Reached as ``threading.Thread`` / ``.Lock`` / ``.Event``.
        "opentelemetry.sdk._shared_internal": {"threading": real},
        # Imported by name at module level.
        "opentelemetry.sdk.metrics._internal.export": {
            name: getattr(real, name) for name in _SDK_THREAD_NAMES
        },
    }

    rebound = False
    for module_name, attributes in rebindings.items():
        try:
            __import__(module_name)
        except ImportError:
            continue  # a trimmed artifact need not ship every signal
        module = sys.modules[module_name]
        for name, value in attributes.items():
            setattr(module, name, value)
        rebound = True
    return rebound


@contextmanager
def _strip_path():
    """Hide this directory from ``PYTHONPATH`` for the duration.

    Anything spawned while it is visible re-runs auto-instrumentation, which
    spawns again.
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
    eventlet is surfaced with :func:`warnings.warn`, not the module logger:
    this runs before logging is configured.

    :returns: ``None``.
    """
    flag = os.environ.get(OTEL_PYTHON_EVENTLET_MONKEY_PATCH) or "false"
    if flag.strip().lower() != "true":
        return

    with _strip_path():
        try:
            eventlet = __import__("eventlet")
            eventlet.monkey_patch()
        except ImportError:
            warnings.warn(
                f"{OTEL_PYTHON_EVENTLET_MONKEY_PATCH} is set to true, but "
                "eventlet is not installed; continuing without "
                "monkey-patching.",
                stacklevel=2,
            )
            return

        # Forced, not setdefault: a stale value would re-introduce the
        # fork/socket mismatch the patch prevents.
        os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
        # Both only make sense once something is green, and both must precede
        # ``initialize()`` building the span processors and registering their
        # fork handlers. ``unpatch_sdk`` imports the SDK, so it has to follow
        # the patch rather than lead it.
        register_forkhook()
        unpatch_sdk()
