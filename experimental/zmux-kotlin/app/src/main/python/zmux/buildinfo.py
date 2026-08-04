"""Build fingerprint for on-device verification.

The CI workflow writes ``build_marker.txt`` (git SHA + workflow run id) into
the app source directory right before packaging; the file is tarred into
``assets/private.tar`` and unpacked to the app's files dir at first launch.
``zmux-info`` and ``gates`` print this marker so a build can be identified on
the phone.

Why this exists: the first "fixed APK still failing" report turned out to be a
stale APK — its ``gates`` output (``[PASS] ptx``) came from code that never
existed in this repository, so the device was running an old build. With a
marker, "which build is on this phone" stops being guesswork.
"""
from __future__ import annotations

from pathlib import Path

_MARKER_FILE = "build_marker.txt"


def marker_path() -> Path:
    """Location of the marker next to the zmux package (the app dir)."""
    return Path(__file__).resolve().parent.parent / _MARKER_FILE


def build_marker() -> str:
    """Return the recorded build marker, or '' when absent (desktop runs)."""
    try:
        return marker_path().read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def short_marker() -> str:
    """Compact marker (the git SHA part) for one-line displays."""
    marker = build_marker()
    if not marker:
        return ""
    return marker.split()[0]
