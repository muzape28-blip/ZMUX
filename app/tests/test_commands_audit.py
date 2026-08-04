"""Command audit — "everything that is called must answer".

Every command ZMUX exposes (built-ins, zpip/zmux commands, the Alpine
sandbox commands) must produce a real result: a dict with an exit code and
string output — never a hang, never a swallowed no-op, never a crash that
kills the worker. These tests freeze that contract so a command added to one
registry without the others fails CI instead of going unnoticed.

Parity invariants checked:
- every builtin has a ``_cmd_<name>`` handler,
- the pty layer knows every command the shell can dispatch,
- ``cli.COMMANDS`` and ``paths.CLI_COMMANDS`` (the BIN_DIR wrappers) match,
- ``cli.main`` returns an int (an answer) for every registered command.
"""
import os
from pathlib import Path

import pytest

from zmux import cli, linuxenv, paths
from zmux.python_shell import PythonShell

#: Commands handled outside the builtin table.
ZMX_COMMANDS = {
    "python", "python3", "pip", "zpip", "help", "zmux-info",
    "zmux-setup-storage", "git", "linux", "alpine", "linux-setup", "gates",
    "zmux-pty-probe",
}

#: Every exposed command with arguments that must answer quickly and
#: deterministically on any host. Network/heavy paths are stubbed below.
PROBES = {
    "ls": [], "mkdir": ["audit_dir"], "touch": ["audit_file"],
    "cp": ["audit_file", "audit_copy"], "mv": ["audit_copy", "audit_moved"],
    "cat": ["audit_file"], "echo": ["halo"], "pwd": [], "cd": ["."],
    "clear": [], "env": [], "which": ["sh"], "uname": [],
    "python": [], "python -c": ["print(1)"],
    "help": [], "zmux-info": [], "zpip": ["list"], "pip": [],
    "zmux-setup-storage": [],
    "linux": ["cat", "/etc/alpine-release"], "alpine": ["cat", "/etc/alpine-release"],
    "git": ["clone", "https://x/y"], "linux-setup": [], "gates": [], "exit": [],
}


@pytest.fixture
def stub_sandbox(monkeypatch):
    """Make the Alpine sandbox deterministic: not installed, installer and
    gates answered with canned results. The installed-path answers have their
    own real integration tests (test_linuxenv.py)."""
    monkeypatch.setattr(linuxenv, "is_installed", lambda: False)
    monkeypatch.setattr(
        linuxenv, "install",
        lambda progress=None: {"ok": True, "already": False,
                               "version": "3.22.5", "path": "/tmp/fake-rootfs"},
    )
    monkeypatch.setattr(
        linuxenv, "run_gates",
        lambda report=None: {"ptmx": {"ok": True, "detail": "stubbed"}},
    )


def test_every_exposed_command_answers(tmp_path, stub_sandbox):
    shell = PythonShell(tmp_path)
    for command, args in PROBES.items():
        line = " ".join([command, *args])
        result = shell.execute(line)
        assert isinstance(result, dict), f"{line!r} must return a result dict"
        assert "exit_code" in result, f"{line!r} must report an exit code"
        assert isinstance(result.get("stdout"), str), f"{line!r} stdout must be str"
        assert isinstance(result.get("stderr"), str), f"{line!r} stderr must be str"
    # The terminal must still answer after the whole sweep.
    assert shell.execute("echo still-alive")["stdout"] == "still-alive\n"


def test_known_answer_shapes(tmp_path, stub_sandbox):
    shell = PythonShell(tmp_path)
    cases = {
        "ls -Z": 1,                 # unknown flag -> loud error
        "definitely-not-a-cmd-9z": 127,   # unknown word -> command not found
        "true && touch x": 2,       # unsupported operator -> loud error
        "git clone https://x/y": 1,     # sandbox not installed -> honest hint
        "linux apk add git": 1,         # same
        "linux-setup": 0,               # installer answers
        "gates": 0,                     # probe answers
        "help": 0, "zmux-info": 0, "zpip list": 0,
    }
    for line, expected in cases.items():
        result = shell.execute(line)
        assert result["exit_code"] == expected, (
            f"{line!r}: expected exit {expected}, got {result['exit_code']}\n"
            f"  stdout={result['stdout']!r}\n  stderr={result['stderr']!r}"
        )
    # Honest hints, not silence.
    git_result = shell.execute("git clone https://x/y")
    assert "linux-setup" in git_result["stderr"]
    help_result = shell.execute("help")
    assert "linux-setup" in help_result["stdout"] and "gates" in help_result["stdout"]

    # Python that merely *contains* shell-operator text must keep working:
    # the loud operator error is for shell intent, not string literals.
    assert shell.execute('print("a && b")')["ok"] is True
    assert shell.execute('x = "a && b"')["ok"] is True
    assert shell.execute("true && touch x")["exit_code"] == 2  # shell intent
    assert shell.execute("false || echo hi")["exit_code"] == 2
    assert shell.execute("true")["exit_code"] == 127           # bare -> not found


def test_builtin_handlers_exist():
    shell = PythonShell("/tmp")
    for name in shell.commands:
        assert callable(getattr(shell, f"_cmd_{name}", None)), (
            f"builtin {name!r} has no _cmd_{name} handler"
        )


class _StubWS:
    def broadcast(self, data: bytes) -> None:
        pass

    def register_callbacks(self, **kwargs) -> None:
        pass


def test_pty_knows_every_shell_command():
    from zmux.pty_session import PTYTerminalSession

    shell = PythonShell("/tmp")
    session = PTYTerminalSession(_StubWS())
    known = session._shell_commands()
    # The pty layer must know every dispatchable command: built-ins, the
    # zmux/pip/git/linux family, and the known-TUI names that are routed to
    # the executor so they render the "needs a real TTY" hint.
    assert set(shell.commands) | ZMX_COMMANDS | set(shell.KNOWN_TUI_COMMANDS) == known


def test_cli_and_wrapper_registry_parity(stub_sandbox, capsys):
    """cli.COMMANDS (in-process dispatch) == paths.CLI_COMMANDS (BIN_DIR
    wrappers), and every registered command returns an int exit code."""
    assert cli.COMMANDS == paths.CLI_COMMANDS
    for name in cli.COMMANDS:
        code = cli.main([name])
        assert isinstance(code, int), f"cli.main({name!r}) must return an int"
        capsys.readouterr()  # swallow output; we only care that it answered


def test_alias_alpine_answers(tmp_path, stub_sandbox):
    shell = PythonShell(tmp_path)
    result = shell.execute("alpine cat /etc/alpine-release")
    assert "exit_code" in result
    assert "linux-setup" in result["stderr"]  # not-installed honest hint
    code = cli.main(["alpine"])
    assert isinstance(code, int)
