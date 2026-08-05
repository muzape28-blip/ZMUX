"""Crash logging for ZMUX.

``main.py`` already writes a crash log when the *main* thread dies, but every
interesting thing ZMUX does happens on a worker thread: the command executor,
the websocket accept loops, the per-client handlers. An exception there was
swallowed by a blanket ``except Exception`` and never reached disk, which
turns a reproducible bug into "the app went weird" — exactly the report you
cannot act on during device testing.

Installs hooks for both thread flavours:

* ``threading.excepthook`` — uncaught exceptions in any ``Thread``;
* ``sys.unraisablehook`` — exceptions Python cannot propagate (``__del__``,
  GC callbacks), which on Android often indicate a resource being torn down
  in the wrong order.

Modelled on ReTerminal's global ``Thread.UncaughtExceptionHandler``.
"""
from __future__ import annotations

import datetime
import sys
import threading
import traceback
from pathlib import Path

#: Keep the log bounded; it lives in app-private storage on a phone.
MAX_LOG_BYTES = 256 * 1024

_installed = False


def _log_path() -> Path:
    from zmux.paths import LOG_DIR
    return Path(LOG_DIR) / "zmux_crash.log"


def record(source: str, exc_type, exc_value, exc_traceback) -> None:
    """Append one formatted crash entry. Never raises."""
    try:
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        body = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        entry = f"\n===== {stamp} [{source}] =====\n{body}"
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate from the front when the log grows past the cap, so the
        # most recent crash is always the one that survives.
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            tail = path.read_text(encoding="utf-8", errors="replace")[-MAX_LOG_BYTES // 2:]
            path.write_text(tail, encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    except Exception:
        # A failure to log must never escalate into a second crash.
        pass


def _thread_hook(args) -> None:
    name = getattr(args.thread, "name", "unknown")
    record(f"thread:{name}", args.exc_type, args.exc_value, args.exc_traceback)


def _unraisable_hook(args) -> None:
    where = getattr(args, "object", None)
    record(f"unraisable:{where!r}", args.exc_type, args.exc_value, args.exc_traceback)


def install() -> None:
    """Install the crash hooks once (idempotent)."""
    global _installed
    if _installed:
        return
    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    _installed = True
