"""Shared-storage access for ZMUX (`zmux-setup-storage`).

ZMUX lives in app-private storage, which nothing else on the phone can read.
That is the right default, but it makes the terminal an island: you cannot
open a file you downloaded, and a script's output cannot be shared.

Termux solves this with ``termux-setup-storage``, which requests the storage
permission and symlinks the usual Android directories into ``~/storage``.
This is the same idea, adapted to ZMUX.

What this changes about ZMUX's threat model
-------------------------------------------
Requesting ``READ_EXTERNAL_STORAGE`` / ``WRITE_EXTERNAL_STORAGE`` means ZMUX
can no longer claim "INTERNET only". That is a real trade-off and it is
**opt-in**: the permission is declared in the manifest but nothing is
requested until the user runs ``zmux-setup-storage``. Until then ZMUX stays
sandboxed exactly as before.

Scoped storage (Android 10+)
----------------------------
On API 29+ the legacy permissions no longer grant blanket filesystem access.
``Context.getExternalFilesDir()`` always works without any permission, so the
app-specific external directory is linked unconditionally; the shared
directories (Downloads, Documents, DCIM…) are linked when the platform
allows. This is why some links may be missing on a modern device — reported
honestly rather than silently.
"""
from __future__ import annotations

import os
from pathlib import Path

from zmux.paths import HOME_DIR

#: Where the symlinks are created (``~/storage``, as in Termux).
STORAGE_DIR = HOME_DIR / "storage"

#: Public directories to expose, as (link name, Android relative path).
PUBLIC_TARGETS = (
    ("downloads", "Download"),
    ("documents", "Documents"),
    ("dcim", "DCIM"),
    ("pictures", "Pictures"),
    ("music", "Music"),
    ("movies", "Movies"),
)


def _is_android() -> bool:
    return any(
        key in os.environ
        for key in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH")
    )


def request_permissions() -> tuple:
    """Ask Android for the storage permissions. Returns (granted, message).

    The request goes through the primed Java bridge (``zmux.javabridge``)
    rather than p4a's ``android.permissions`` module: that module calls
    ``autoclass('org.kivy.android.PythonActivity')`` on the calling thread,
    which fails from ZMUX's command-executor worker thread (JNI FindClass
    falls back to the system class loader on threads without app stack
    frames — see zmux.javabridge). The bridge resolves the class once on the
    main thread at startup, so the cached wrapper is safe to use here.

    Off-device this is a no-op so the rest of the flow stays testable.
    """
    if not _is_android():
        return True, "not running on Android; skipping the permission prompt"
    try:
        from zmux import javabridge
        mActivity = javabridge.mActivity()
        if mActivity is None:
            reason = javabridge.error() or "Java bridge unavailable"
            return False, (
                f"could not reach the Android activity ({reason}) — grant "
                "Storage manually in Android Settings > Apps > ZMUX"
            )
        # Fire-and-forget: the system dialog is asynchronous, results (if any)
        # land on onRequestPermissionsResult and are not needed here — setup()
        # reports what actually became reachable afterwards. On Android 10+
        # (API 29+) the manifest only declares the legacy storage permissions
        # with maxSdkVersion=28, so this prompt is a no-op there anyway and
        # the app-external directory below is linked without any permission.
        mActivity.requestPermissions(
            [
                "android.permission.READ_EXTERNAL_STORAGE",
                "android.permission.WRITE_EXTERNAL_STORAGE",
            ]
        )
    except Exception as error:  # never crash the terminal over a prompt
        return False, f"permission request failed: {error}"
    # The dialog is asynchronous: the user may still be deciding. Report what
    # actually became reachable rather than assuming success.
    return True, "permission requested"


def external_roots() -> list:
    """Candidate shared-storage roots for this device, most reliable first."""
    roots = []
    for value in (
        os.environ.get("EXTERNAL_STORAGE"),
        os.environ.get("ANDROID_STORAGE"),
        "/storage/emulated/0",
        "/sdcard",
    ):
        if value:
            path = Path(value)
            if path.is_dir() and path not in roots:
                roots.append(path)
    return roots


def _link(name: str, target: Path, results: list) -> None:
    """Create ``~/storage/<name>`` -> ``target``; record the outcome."""
    link = STORAGE_DIR / name
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target, target_is_directory=True)
        readable = os.access(target, os.R_OK)
        results.append((name, str(target), "ok" if readable else "linked (not readable yet)"))
    except OSError as error:
        results.append((name, str(target), f"failed: {error}"))


def setup() -> dict:
    """Create ``~/storage`` and link the reachable Android directories."""
    granted, message = request_permissions()
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    results: list = []

    # 1. App-specific external dir: always allowed, no permission needed.
    app_external = os.environ.get("ANDROID_APP_PATH") or os.environ.get("EXTERNAL_STORAGE")
    if app_external:
        candidate = Path(app_external)
        if candidate.is_dir():
            _link("app", candidate, results)

    # 2. Public shared directories, when the platform exposes them.
    roots = external_roots()
    if roots:
        root = roots[0]
        _link("shared", root, results)
        for name, relative in PUBLIC_TARGETS:
            target = root / relative
            if target.is_dir():
                _link(name, target, results)
    return {
        "ok": bool(results),
        "granted": granted,
        "message": message,
        "storage_dir": str(STORAGE_DIR),
        "links": results,
        "android": _is_android(),
    }


def format_setup(result: dict) -> str:
    """Render :func:`setup` for the terminal."""
    lines = ["ZMUX storage setup", "=" * 40]
    if not result.get("android"):
        lines.append("Not running on Android — links point at this machine's paths.")
    lines.append(f"Permission: {result.get('message', 'unknown')}")
    lines.append(f"Location:   {result['storage_dir']}")
    if not result.get("links"):
        lines += [
            "",
            "No shared directories were reachable.",
            "On Android 11+ scoped storage may block them even after granting",
            "the permission. Files can still be exchanged through the app's",
            "own external directory, when the device provides one.",
        ]
        return "\n".join(lines)
    lines.append("")
    for name, target, status in result["links"]:
        lines.append(f"  ~/storage/{name:<10} -> {target}  [{status}]")
    lines += ["", "Use them like any other directory, e.g.  ls ~/storage/downloads"]
    return "\n".join(lines)
