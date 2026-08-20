"""Eventlet monkey-patch, factored out so it can run at the earliest hook."""

import os
import re
import warnings

__all__ = [
    "OTEL_PYTHON_EVENTLET_MONKEY_PATCH",
    "OTEL_PYTHON_OSLO_SERVICE_BACKEND",
    "try_patch",
]

#: Opt-in flag: when ``"true"`` (case-insensitive), eventlet is monkey-patched.
OTEL_PYTHON_EVENTLET_MONKEY_PATCH = "OTEL_PYTHON_EVENTLET_MONKEY_PATCH"

#: Backend forced for oslo.service once eventlet has been patched.
OTEL_PYTHON_OSLO_SERVICE_BACKEND = "OTEL_PYTHON_OSLO_SERVICE_BACKEND"


def try_patch() -> None:
    """Monkey-patch eventlet when opted in, and pin the oslo.service backend.

    No-op unless ``OTEL_PYTHON_EVENTLET_MONKEY_PATCH`` is ``"true"``
    (case-insensitive). A missing ``eventlet`` is surfaced with
    :func:`warnings.warn` rather than the module logger on purpose: this runs
    during startup, before the logging pipeline is configured, so a warning is
    the reliably visible channel for the misconfiguration.

    :returns: ``None``.
    """
    flag = os.environ.get(OTEL_PYTHON_EVENTLET_MONKEY_PATCH) or "false"
    if flag.strip().lower() != "true":
        return

    filedir = os.path.dirname(os.path.abspath(__file__))

    python_path = os.environ.get("PYTHONPATH")
    auto_instrumentation_path_was_present = (
        python_path is not None
        and filedir in python_path.split(os.path.pathsep)
    )

    # Remove the auto-instrumentation path during initialization to prevent
    # auto-instrumentation from executing in subprocesses spawned during this phase.
    # This suppression is performed to avoid creating a recursive loop scenario
    # where subprocesses spawned in the initialization phase execute the
    # initialization phase again, spawning more subprocesses.
    if python_path is not None:
        os.environ["PYTHONPATH"] = re.sub(
            rf"{re.escape(filedir)}{os.path.pathsep}(?!$)",
            "",
            python_path,
        )

    try:
        eventlet = __import__("eventlet")
        eventlet.monkey_patch()
        # Forced (not ``setdefault``): a stale threading value would
        # re-introduce the fork/socket mismatch the patch prevents.
        os.environ[OTEL_PYTHON_OSLO_SERVICE_BACKEND] = "eventlet"
    except ImportError:
        warnings.warn(
            f"{OTEL_PYTHON_EVENTLET_MONKEY_PATCH} is set to true, but "
            "eventlet is not installed; continuing without "
            "monkey-patching.",
            stacklevel=2,
        )
    finally:
        if auto_instrumentation_path_was_present:
            current = os.environ.get("PYTHONPATH", "")
            if filedir not in current.split(os.path.pathsep):
                os.environ["PYTHONPATH"] = (
                    filedir + os.path.pathsep + current if current else filedir
                )
