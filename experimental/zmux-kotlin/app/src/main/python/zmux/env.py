"""Child-process environment construction for ZMUX.

One shared builder used by every code path that spawns a child process, so
the REST session and the interactive terminal can never drift apart.

Why this module exists
----------------------
``TerminalSession`` used to build a careful environment and then never pass
it to anything: the only ``subprocess.Popen`` in the codebase ran without
``env=``, so children inherited the raw app environment. Centralising the
builder here makes the omission impossible to repeat.

Android specifics (informed by Termux/ReTerminal session setup)
---------------------------------------------------------------
Android system binaries are not self-contained: ``/system/bin/sh`` and
friends read a set of ``ANDROID_*`` / ``BOOTCLASSPATH`` variables that the
zygote exports into every app process. Those are inherited automatically by
``os.environ.copy()``; :data:`ANDROID_PASSTHROUGH` documents the contract and
is used by :func:`describe_passthrough` for diagnostics (``zmux-info``).
"""
from __future__ import annotations

import os
from pathlib import Path

from zmux.paths import (
    APP_DIR,
    BIN_DIR,
    HOME_DIR,
    android_exec_blocked,
    legacy_user_packages_pythonpath,
)

#: Directories always present on PATH, after BIN_DIR. On Android the inherited
#: PATH is frequently minimal or absent, which makes basic utilities fail with
#: a confusing "not found".
SYSTEM_PATHS = (
    "/system/bin",
    "/system/xbin",
    "/vendor/bin",
    "/sbin",
    "/bin",
    "/usr/bin",
)

#: Variables exported by the Android zygote that system binaries rely on.
#: Inherited via os.environ.copy(); listed for documentation + diagnostics.
ANDROID_PASSTHROUGH = (
    "ANDROID_ART_ROOT",
    "ANDROID_DATA",
    "ANDROID_I18N_ROOT",
    "ANDROID_ROOT",
    "ANDROID_RUNTIME_ROOT",
    "ANDROID_TZDATA_ROOT",
    "ANDROID_STORAGE",
    "BOOTCLASSPATH",
    "DEX2OATBOOTCLASSPATH",
    "EXTERNAL_STORAGE",
)


def build_path(extra: tuple = ()) -> str:
    """Return a de-duplicated PATH: BIN_DIR, then system dirs, then inherited.

    Under Android's W^X rule (:func:`zmux.paths.android_exec_blocked`) BIN_DIR
    is *omitted*: execve() of app-private files is kernel-denied, so a PATH
    entry pointing at the generated wrappers could only resolve commands into
    guaranteed ``Permission denied`` errors (e.g. a child ``/system/bin/sh``
    resolving ``clear`` to the ZMUX wrapper instead of the toybox applet).
    On desktop/CI the wrappers execute normally and stay first on PATH.
    """
    seen: set = set()
    parts: list = []
    inherited = os.environ.get("PATH", "")
    blocked = android_exec_blocked()
    head = () if blocked else (str(BIN_DIR),)
    app_prefix = str(APP_DIR) + os.path.sep
    for entry in (
        *head,
        *extra,
        *SYSTEM_PATHS,
        *(inherited.split(os.pathsep) if inherited else []),
    ):
        if blocked and entry and (entry == str(BIN_DIR) or entry.startswith(app_prefix)):
            # An inherited entry into the non-executable app sandbox can only
            # manufacture "Permission denied" — drop it no matter how it got
            # in (stale prepend, embedding host, hand-built environment).
            continue
        if entry and entry not in seen:
            seen.add(entry)
            parts.append(entry)
    return os.pathsep.join(parts)


def build_env(cwd: Path | None = None) -> dict:
    """Build the environment handed to ZMUX child processes."""
    env = os.environ.copy()
    env["HOME"] = str(HOME_DIR)
    env["PWD"] = str(cwd or HOME_DIR)
    # A real terminal identity: xterm.js speaks xterm-256color and the
    # front-end renders 24-bit colour, so advertise both. Without TERM the
    # curses/readline-based tools fall back to dumb mode.
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    env["PATH"] = build_path()
    env["TMPDIR"] = str(Path(HOME_DIR).parent / "cache")
    # Legacy zpip packages remain importable by host-side compatibility child
    # interpreters. Alpine-first package work uses apk and venv-local pip.
    env["PYTHONPATH"] = legacy_user_packages_pythonpath(env.get("PYTHONPATH", ""))
    env["ZMUX"] = "1"
    return env


def describe_passthrough() -> dict:
    """Report which Android passthrough variables are actually present."""
    return {name: os.environ.get(name) for name in ANDROID_PASSTHROUGH}
