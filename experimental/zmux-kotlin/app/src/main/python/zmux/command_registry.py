"""Command registry for ZMUX app-control wrappers.

The Alpine PTY shell is the product interface. These names are only the small
set of app-control commands that may be exposed through generated wrappers or
accepted by ``python -m zmux.cli`` for compatibility.
"""
from __future__ import annotations

#: User-safe app controls that remain part of the supported surface.
PRIMARY_COMMANDS = (
    "help",
    "clear",
    "zmux-setup-storage",
    "linux-setup",
)

#: App diagnostics. Useful for maintainers; not a package workflow.
DIAGNOSTIC_COMMANDS = (
    "zmux-info",
    "gates",
    "zmux-pty-probe",
)

#: Legacy host-side compatibility commands. Keep wrappers for now so older
#: installs/scripts get an answer, but do not promote these as normal UX.
LEGACY_COMPAT_COMMANDS = (
    "zpip",
    "pip",
    "linux",
)

#: Accepted legacy aliases that are not generated as wrappers anymore.
LEGACY_ALIAS_COMMANDS = (
    "alpine",
)

#: Commands that get executable scripts in BIN_DIR.
WRAPPER_COMMANDS = PRIMARY_COMMANDS + DIAGNOSTIC_COMMANDS + LEGACY_COMPAT_COMMANDS

#: Everything the in-process CLI dispatcher accepts.
ACCEPTED_COMMANDS = WRAPPER_COMMANDS + LEGACY_ALIAS_COMMANDS


def command_category(command: str) -> str:
    """Return ``primary``, ``diagnostic``, ``legacy`` or ``unknown``."""
    if command in PRIMARY_COMMANDS:
        return "primary"
    if command in DIAGNOSTIC_COMMANDS:
        return "diagnostic"
    if command in LEGACY_COMPAT_COMMANDS or command in LEGACY_ALIAS_COMMANDS:
        return "legacy"
    return "unknown"
