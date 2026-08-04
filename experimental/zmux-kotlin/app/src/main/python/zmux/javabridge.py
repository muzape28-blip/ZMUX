"""Android Java bridge that is safe to use from any ZMUX thread.

Why this exists
---------------
ZMUX executes commands on a dedicated Python worker thread (and the REST
server uses more threads). On Android, JNI ``FindClass`` — which pyjnius's
``autoclass()`` calls — resolves app-specific classes through the *system*
class loader when invoked from a thread that has no application Java stack
frames (a Python-created thread attached via ``AttachCurrentThread``). The
Android docs are explicit about this:

    "You can get into trouble if you create a thread yourself (perhaps by
    calling pthread_create and then attaching it with AttachCurrentThread).
    Now there are no stack frames from your application. If you call
    FindClass from this thread, the JavaVM will start in the 'system' class
    loader instead of the one associated with your application, so attempts
    to find app-specific classes will fail."
    — https://developer.android.com/training/articles/perf-jni#faq_FindClass

That is exactly the failure reported on-device by ``zmux-setup-storage``:

    [session error] JVM exception occurred: java.lang.ClassNotFoundException:
    Didn't find class "org.kivy.android.PythonActivity" on path:
    DexPathList[[directory "."],nativeLibraryDirectories=[/system/lib, ...]]

p4a's own ``android.permissions`` / ``android.runnable`` modules call
``autoclass('org.kivy.android.PythonActivity')`` unconditionally, so they
inherit the same failure on every worker thread.

pyjnius caches resolved classes (``MetaJavaClass`` registry), so the fix is
to resolve the app-owned classes **once, early, on the Python main thread**
(where the ``PythonMain.run`` Java frame is on the stack and the app class
loader is in effect). Every later ``autoclass()`` from any thread then
returns the cached wrapper without touching ``FindClass``.

The webview bootstrap starts Python on a dedicated ``PythonThread`` and the
Android UI thread stays free, so this module's accessors are safe to call
from anywhere without blocking the UI.
"""
from __future__ import annotations

import os

#: The p4a activity that hosts the ZMUX Python runtime. Same constant p4a
#: generates into ``android.config`` (ACTIVITY_CLASS_NAMESPACE + NAME).
ACTIVITY_CLASS_NAME = "org.kivy.android.PythonActivity"

#: Cached Java class wrapper once :func:`prime` succeeded. pyjnius returns
#: this same object from ``autoclass`` afterwards (MetaJavaClass registry).
_activity_class = None

#: Human-readable reason when the bridge is unavailable (desktop, or the
#: APK was built without the expected activity).
_bridge_error: str | None = None
_primed = False


def _is_android() -> bool:
    return any(
        key in os.environ
        for key in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH")
    )


def prime() -> bool:
    """Resolve the app's Java classes while FindClass still works.

    Must be called once, at startup, from the Python main thread (see the
    module docstring for why). Idempotent and thread-safe in the sense that
    repeated calls converge on the same outcome; the first caller wins.

    Returns True when the activity class is available, False otherwise
    (desktop runs, missing pyjnius, or a genuinely broken APK).
    """
    global _activity_class, _bridge_error, _primed
    if _primed:
        return _activity_class is not None
    _primed = True
    if not _is_android():
        _bridge_error = "not running on Android"
        return False
    try:
        from jnius import autoclass  # type: ignore

        _activity_class = autoclass(ACTIVITY_CLASS_NAME)
        # Touch the static mActivity field while we are still on a thread
        # whose class loader can see it, so the field lookup is also cached.
        _activity_class.mActivity
        return True
    except Exception as error:  # pragma: no cover - only happens on-device
        _activity_class = None
        _bridge_error = (
            f"could not resolve {ACTIVITY_CLASS_NAME}: "
            f"{type(error).__name__}: {error}"
        )
        return False


def activity_class():
    """Return the cached ``org.kivy.android.PythonActivity`` class, or None.

    Safe from any thread *after* :func:`prime` has run (startup). Calling
    before prime from a worker thread will attempt a raw ``autoclass`` and
    may fail on Android — callers should treat None as "bridge unavailable"
    and degrade gracefully.
    """
    if not _primed:
        prime()
    return _activity_class


def mActivity():
    """Return the live Android activity instance, or None when unavailable."""
    cls = activity_class()
    if cls is None:
        return None
    try:
        return cls.mActivity
    except Exception:  # pragma: no cover - only happens on-device
        return None


def error() -> str | None:
    """Human-readable reason the bridge is unavailable (for diagnostics)."""
    return _bridge_error
