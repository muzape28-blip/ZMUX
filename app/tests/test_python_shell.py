"""Regression tests for the Python-native terminal executor."""
import threading
import time
from pathlib import Path

import pytest

from zmux.python_shell import PythonShell


def test_python_expression_executes_in_persistent_repl(tmp_path: Path):
    shell = PythonShell(tmp_path)
    assert shell.execute("value = 21")["ok"]
    result = shell.execute("value * 2")
    assert result["ok"]
    assert result["stdout"] == "42\n"


def test_filesystem_commands_are_real(tmp_path: Path):
    shell = PythonShell(tmp_path)
    assert shell.execute("mkdir project")["ok"]
    assert shell.execute("touch project/main.py")["ok"]
    assert (tmp_path / "project" / "main.py").is_file()
    assert "main.py" in shell.execute("ls project")["stdout"]


def test_pipeline_and_redirect_do_not_require_a_shell(tmp_path: Path):
    shell = PythonShell(tmp_path)
    # echo/cat are real system programs for this syntax; PythonShell starts
    # them directly and owns all pipe/redirection wiring.
    result = shell.execute("echo native > output.txt")
    assert result["ok"]
    result = shell.execute("cat output.txt | grep native")
    assert result["ok"]
    assert result["stdout"] == "native\n"


def test_unknown_command_with_pipe_still_reaches_subprocess(tmp_path: Path):
    """Commands containing |, >, < must route to the pipeline executor even
    when the first word is not a known executable (regression guard for the
    operator-routing branch)."""
    shell = PythonShell(tmp_path)
    result = shell.execute("definitely-not-a-real-cmd-9z | cat")
    assert not result["ok"]
    assert result["exit_code"] == 127
    assert "command not found" in result["stderr"]


class TestRmFlags:
    """`rm` option parsing must be strict: a stray "-" argument that merely
    *contains* the letters r/f previously enabled recursive+force silently."""

    def test_rejects_deceptive_option_string(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("mkdir targeted")
        shell.execute("touch targeted/innocent.txt")
        result = shell.execute("rm -random-flag targeted")
        assert not result["ok"]
        assert "invalid option" in result["stderr"]
        # The directory must survive untouched.
        assert (tmp_path / "targeted" / "innocent.txt").is_file()

    def test_rejects_unknown_single_letter(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("touch keep.txt")
        result = shell.execute("rm -v keep.txt")
        assert not result["ok"]
        assert "invalid option" in result["stderr"]
        assert (tmp_path / "keep.txt").is_file()

    def test_accepts_valid_flag_forms(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        for flags in ("-r", "-R", "-rf", "-fr", "-Rf", "--recursive", "--recursive --force"):
            shell.execute("mkdir victim")
            shell.execute("touch victim/file.txt")
            result = shell.execute(f"rm {flags} victim")
            assert result["ok"], flags
            assert not (tmp_path / "victim").exists()

    def test_force_suppresses_missing_file(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        assert shell.execute("rm -f does-not-exist.txt")["ok"]
        assert shell.execute("rm --force also-missing.txt")["ok"]

    def test_missing_operand_rejected(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        result = shell.execute("rm")
        assert not result["ok"]
        assert "missing operand" in result["stderr"]

    def test_refuses_directory_without_recursive(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("mkdir plain")
        result = shell.execute("rm plain")
        assert not result["ok"]
        assert (tmp_path / "plain").is_dir()


def test_which_resolves_each_name_exactly_once(tmp_path: Path, monkeypatch):
    shell = PythonShell(tmp_path)
    calls = []

    def fake_find(command):
        calls.append(command)
        return "/system/bin/cat" if command == "cat" else None

    monkeypatch.setattr(shell, "_find_executable", fake_find)
    result = shell.execute("which cat")
    assert result["ok"]
    assert result["stdout"] == "/system/bin/cat\n"
    assert calls == ["cat"], f"expected 1 lookup, got {len(calls)}"


class TestInterrupt:
    """Cooperative Ctrl+C: interrupt() must unblock a running pipeline."""

    def test_interrupt_kills_running_subprocess(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        outcome = {}

        def _run():
            outcome["result"] = shell.execute("sleep 30")

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        # Wait until the process is actually registered, then interrupt.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not shell._procs:
            time.sleep(0.05)
        assert shell._procs, "pipeline process never registered"

        started = time.monotonic()
        shell.interrupt()
        worker.join(timeout=15)
        assert not worker.is_alive(), "pipeline thread still blocked after interrupt"
        assert time.monotonic() - started < 15
        assert outcome["result"]["exit_code"] != 0

    def test_clear_interrupt_resets_latch(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.interrupt()
        assert shell._interrupt.is_set()
        shell.clear_interrupt()
        assert not shell._interrupt.is_set()


class TestCleanControlFlowRendering:
    """Ctrl+C and sys.exit must not dump traceback spam (real-REPL semantics)."""

    def test_keyboard_interrupt_is_one_line_130(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        result = shell._exec_python("raise KeyboardInterrupt")
        assert result["exit_code"] == 130
        assert "KeyboardInterrupt" in result["stderr"]
        assert "Traceback" not in result["stderr"]

    def test_system_exit_is_quiet_and_keeps_code(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        result = shell._exec_python("import sys; sys.exit(7)")
        assert result["exit_code"] == 7
        assert "Traceback" not in result["stderr"]

    def test_force_python_bypasses_command_builtins(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        result = shell.execute("ls", force_python=True)
        assert not result["ok"]
        assert "NameError" in result["stderr"]


class TestRichTraceback:
    def test_rich_rendering_when_rich_installed(self, tmp_path: Path, monkeypatch):
        pytest.importorskip("rich")
        import zmux.python_shell as python_shell

        monkeypatch.setattr(python_shell, "_rich_impl", python_shell._RICH_UNSET)
        shell = PythonShell(tmp_path)
        result = shell._exec_python("1/0")
        assert "ZeroDivisionError" in result["stderr"]
        assert "\x1b[" in result["stderr"], "rich traceback should carry ANSI styling"

    def test_plain_traceback_without_rich(self, tmp_path: Path, monkeypatch):
        import zmux.python_shell as python_shell

        monkeypatch.setattr(python_shell, "_rich_impl", None)  # simulate absence
        shell = PythonShell(tmp_path)
        result = shell._exec_python("1/0")
        assert "Traceback (most recent call last)" in result["stderr"]
        assert "\x1b[" not in result["stderr"]


# ---------------------------------------------------------------------------
# ls strictness: flags are implemented or rejected — never silently swallowed.
# `ls -R` and `ls -t` were previously accepted and ignored (exit 0, plain
# output), the same silent-failure class as the operator guards.
# ---------------------------------------------------------------------------

class TestLsStrictness:
    def test_ls_rejects_unknown_flags_loudly(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("mkdir d")
        result = shell.execute("ls -Z")
        assert not result["ok"]
        assert result["exit_code"] == 1
        assert "invalid option" in result["stderr"]

    def test_ls_does_not_eat_long_unknown_flags(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        result = shell.execute("ls --color")
        assert not result["ok"]
        assert "invalid option" in result["stderr"]

    def test_ls_recursive(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("mkdir -p tree/sub/deep")
        shell.execute("touch tree/a.txt tree/sub/b.txt tree/sub/deep/c.txt")
        result = shell.execute("ls -R tree")
        assert result["ok"]
        out = result["stdout"]
        assert "a.txt" in out and "b.txt" in out and "c.txt" in out
        assert f"{tmp_path / 'tree'}:" in out          # top-level header
        assert f"{tmp_path / 'tree' / 'sub'}:" in out  # nested header
        assert out.index("b.txt") < out.index("c.txt")  # depth-first order

    def test_ls_time_sort_and_reverse(self, tmp_path: Path):
        import os as _os
        shell = PythonShell(tmp_path)
        shell.execute("touch old.txt mid.txt new.txt")
        for name, ts in (("old.txt", 1_600_000_000),
                         ("mid.txt", 1_700_000_000),
                         ("new.txt", 1_800_000_000)):
            _os.utime(tmp_path / name, (ts, ts))
        assert shell.execute("ls -t")["stdout"].split() == \
            ["new.txt", "mid.txt", "old.txt"]
        assert shell.execute("ls -tr")["stdout"].split() == \
            ["old.txt", "mid.txt", "new.txt"]

    def test_ls_multiple_operands_get_headers(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("mkdir a b")
        shell.execute("touch a/x b/y")
        result = shell.execute("ls a b")
        assert result["ok"]
        assert f"{tmp_path / 'a'}:" in result["stdout"]
        assert f"{tmp_path / 'b'}:" in result["stdout"]
        assert "x" in result["stdout"] and "y" in result["stdout"]

    def test_ls_double_dash_lists_dash_file(self, tmp_path: Path):
        shell = PythonShell(tmp_path)
        shell.execute("open('-dash', 'w').close()")
        shell.execute("open('.hidden', 'w').close()")
        plain = shell.execute("ls")["stdout"]
        assert "-dash" in plain and ".hidden" not in plain
        assert ".hidden" in shell.execute("ls -a")["stdout"]
        # `-dash` starts with '-' so it must be reachable via `--`.
        result = shell.execute("ls -- -dash")
        assert result["ok"]
        assert "-dash" in result["stdout"]


# ---------------------------------------------------------------------------
# Streaming invariant: installing the live output sink must never make a
# built-in command's output disappear from the result dict.
# ---------------------------------------------------------------------------

def test_builtin_output_survives_with_sink_installed(tmp_path: Path):
    shell = PythonShell(tmp_path)
    seen = []
    shell.output_sink = lambda data: seen.append(data)
    try:
        result = shell.execute("echo halo")
    finally:
        shell.output_sink = None
    assert result["ok"]
    assert result["stdout"] == "halo\n"  # text still in the result dict
    assert result["streamed"] == ()       # builtins do not touch the sink


# ---------------------------------------------------------------------------
# Scrollback: 32 KiB used to truncate long outputs; 1 MiB is the new floor.
# ---------------------------------------------------------------------------

class _StubWebSocket:
    def broadcast(self, data: bytes) -> None:
        pass


def test_scrollback_holds_one_megibyte():
    from zmux.pty_session import PTYTerminalSession

    session = PTYTerminalSession(_StubWebSocket())
    assert session.scrollback_max_size == 1 << 20
    session._emit(b"A" * 900_000)
    session._emit(b"B" * 900_000)  # 1.8 MiB total — must trim to the tail 1 MiB
    scrollback = session.get_scrollback()
    assert len(scrollback) == 1 << 20
    # The trim keeps the *tail*: the B run (the newest 900 KiB) must be intact.
    assert scrollback[-900_000:] == b"B" * 900_000


# ---------------------------------------------------------------------------
# Prompt tidiness: a prompt must never be pasted onto a command's
# unterminated output tail (`print(1, end="")` then prompt -> new line).
# ---------------------------------------------------------------------------

class _StubWS2:
    def __init__(self):
        self.frames = []

    def broadcast(self, data: bytes) -> None:
        self.frames.append(data)


def test_prompt_starts_on_fresh_line_after_unterminated_output():
    from zmux.pty_session import PTYTerminalSession

    ws = _StubWS2()
    session = PTYTerminalSession(ws)
    session._emit(b"partial-output")   # no trailing newline
    session._emit_prompt()
    assert b"".join(ws.frames).endswith(b"\r\nzmux:~$ ")


def test_prompt_stays_on_same_line_after_newline_terminated_output():
    from zmux.pty_session import PTYTerminalSession

    ws = _StubWS2()
    session = PTYTerminalSession(ws)
    session._emit(b"complete\r\n")
    session._emit_prompt()
    assert b"".join(ws.frames).endswith(b"\r\nzmux:~$ ")
    assert not b"\r\n\r\n" in b"".join(ws.frames)


class TestKnownTuiCommands:
    """Full-screen TUIs get an honest explanation, not a Python NameError.

    Regression: `zpip install nano` succeeded (it installed PyPI's Django
    library) but typing `nano` leaked a confusing `NameError: name 'nano' is
    not defined`. ZMUX has no PTY, so even a real TUI binary cannot render;
    the shell must say so.
    """

    def test_nano_reports_no_tty(self, tmp_path, monkeypatch):
        shell = PythonShell(tmp_path)
        monkeypatch.setattr(shell, "_find_executable", lambda cmd: None)
        result = shell.execute("nano")
        assert not result["ok"]
        assert result["exit_code"] == 1
        assert "real TTY" in result["stderr"]
        assert "no PTY" in result["stderr"]

    def test_known_tui_names_are_covered(self, tmp_path, monkeypatch):
        shell = PythonShell(tmp_path)
        monkeypatch.setattr(shell, "_find_executable", lambda cmd: None)
        for name in ("vim", "vi", "htop", "less", "micro"):
            result = shell.execute(name)
            assert not result["ok"], name
            assert "real TTY" in result["stderr"], name

    def test_non_tui_unknown_word_still_falls_to_python(self, tmp_path):
        # An expression is unambiguously Python (the command-word classifier
        # must not divert it), so it must surface a real NameError.
        shell = PythonShell(tmp_path)
        result = shell.execute("definitely_an_undefined_name_zz9 + 1")
        assert not result["ok"]
        assert "NameError" in result["stderr"]

    def test_tui_names_route_through_command_executor(self, tmp_path):
        from zmux.pty_session import PTYTerminalSession

        class _StubWS:
            def broadcast(self, data: bytes) -> None:
                pass

            def register_callbacks(self, **kwargs) -> None:
                pass

        # The pty layer must route TUI names to the executor (hint path)
        # instead of the Python REPL path.
        session = PTYTerminalSession(_StubWS())
        assert "nano" in session._shell_commands()


# ---------------------------------------------------------------------------
# Long-running tools (git clone, apk, curl) write their progress to stderr.
# The subprocess executor must stream stderr live like stdout, and must never
# hang on a finished process whose child kept the pipe open.
# ---------------------------------------------------------------------------

def test_subprocess_streams_stderr_live(tmp_path: Path):
    # /usr/bin/python3 (absolute path) is a real subprocess; bare `python3`
    # is ZMUX's embedded runtime and would route into _exec_python instead.
    shell = PythonShell(tmp_path)
    seen = []
    shell.output_sink = lambda data: seen.append(data)
    try:
        result = shell.execute(
            '/usr/bin/python3 -c "import sys; print(1); print(2, file=sys.stderr)"'
        )
    finally:
        shell.output_sink = None
    assert result["ok"], result["stderr"]
    assert result["stdout"] == "1\n"
    assert result["stderr"] == "2\n"
    # Both streams reached the terminal sink AND are marked streamed so the
    # session layer does not print them a second time.
    assert result["streamed"] == ("stdout", "stderr")
    rendered = b"".join(seen).decode("utf-8", "replace")
    assert "1" in rendered and "2" in rendered


def test_finished_process_with_stray_pipe_holder_does_not_hang(tmp_path: Path):
    """A child that inherits stdout keeps the pipe open after the parent
    exits; the executor must return instead of blocking on readline forever
    (this is what made a finished `git clone` look permanently stuck)."""
    shell = PythonShell(tmp_path)
    source = (
        "import subprocess, sys; "
        "p = subprocess.Popen(['sleep', '30'], stdout=sys.stdout, stderr=sys.stderr); "
        "print('done')"
    )
    # Run in a watchdog thread: the old implementation (reader.join(None))
    # never returned; the fix must complete well within the budget.
    result_box = {}

    def runner():
        result_box["result"] = shell.execute(
            f'/usr/bin/python3 -c "{source}"', timeout=15
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "executor hung on a stray pipe holder"
    result = result_box["result"]
    assert result["ok"], result["stderr"]
    assert "done" in result["stdout"]


# ---------------------------------------------------------------------------
# Bare `cd` (go home) must work even when HOME_DIR is reached through a
# symlink — Android exposes app storage as /data/user/0/... which resolves to
# /data/data/...; comparing the raw path against its own .resolve() used to
# fail and lock the user out of their home directory.
# ---------------------------------------------------------------------------

def test_bare_cd_returns_home_through_symlink(tmp_path: Path, monkeypatch):
    from zmux import python_shell as ps
    real_home = tmp_path / "real_home"
    real_home.mkdir()
    link_home = tmp_path / "link_home"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setattr(ps, "HOME_DIR", link_home)

    shell = PythonShell(real_home)
    result = shell.execute("cd")
    assert result["ok"], result["stderr"]
    assert shell.cwd == real_home  # resolved to the real directory

    # cd into a subdir and back home again
    (real_home / "sub").mkdir()
    assert shell.execute("cd sub")["ok"]
    assert shell.cwd == real_home / "sub"
    assert shell.execute("cd")["ok"]
    assert shell.cwd == real_home


def test_cd_outside_home_still_blocked(tmp_path: Path):
    shell = PythonShell(tmp_path)
    outside = tmp_path / ".." / "outside"
    outside.mkdir(exist_ok=True)
    result = shell.execute(f"cd {outside.resolve()}")
    assert not result["ok"]
    assert "outside home directory" in result["stderr"]
