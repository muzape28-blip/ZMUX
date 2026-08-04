"""Guardrails for quarantining the legacy host-console/PythonShell path."""

import re


def test_python_shell_is_marked_as_legacy_compatibility_executor():
    from zmux import python_shell

    assert python_shell.LEGACY_COMPATIBILITY_EXECUTOR is True
    assert "Legacy host-side Python command interpreter" in (python_shell.__doc__ or "")
    assert "Alpine PTY shell is the product terminal" in (python_shell.__doc__ or "")
    assert "not the normal user-facing ZMUX shell" in (python_shell.PythonShell.__doc__ or "")


def test_terminal_session_is_marked_as_legacy_rest_executor():
    from zmux import terminal

    assert terminal.LEGACY_REST_EXECUTOR is True
    assert "Legacy REST API terminal session" in (terminal.__doc__ or "")
    assert "WebSocket-backed Alpine PTY session" in (terminal.__doc__ or "")
    assert "Legacy REST session" in (terminal.TerminalSession.__doc__ or "")


def test_pty_session_declares_alpine_as_product_shell():
    from zmux import pty_session

    assert pty_session.ALPINE_PRODUCT_SHELL is True
    assert pty_session.LEGACY_APP_CONTROL_CONSOLE is True
    assert b"legacy compatibility" in pty_session.LEGACY_PTY_TOGGLE_WARNING
    assert "only Alpine is the product shell" in (pty_session.__doc__ or "")
    assert "legacy app-control console" in (pty_session.PTYTerminalSession.toggle_pty.__doc__ or "")


def test_user_help_does_not_advertise_embedded_python_repl_as_main_flow():
    from zmux.terminal import HELP_TEXT

    assert "embedded ZMUX runtime" not in HELP_TEXT
    assert "Python REPL" not in HELP_TEXT
    assert ">>>" not in HELP_TEXT
    assert "apk add <package>" in HELP_TEXT
    assert "python3 -m venv" in HELP_TEXT


def test_terminal_html_does_not_expose_legacy_pty_toggle_button():
    from pathlib import Path
    from zmux.server import BASE_DIR

    html = (Path(BASE_DIR) / "templates" / "terminal.html").read_text(encoding="utf-8")
    # The websocket action is still accepted for compatibility, but no toolbar
    # key should invite users into the legacy host console.
    assert "pty.toggle" in html
    assert not re.search(r"\{\s*label:[^}]+action:\s*['\"]pty\.toggle['\"]", html)
    assert "ZMX⇄" not in html
    assert "jump between Alpine PTY shell and host console" not in html
    assert "host console" not in html
