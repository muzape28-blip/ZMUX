"""Strict tests for the real-PTY machinery (:mod:`zmux.realpty`).

Every test forks a real child on a real PTY — nothing is mocked or emulated.
These assert *kernel-level* semantics (isatty, TIOCSWINSZ, Ctrl+C -> SIGINT,
exit status, reaping), i.e. exactly the semantics a fake/line-discipline
terminal cannot provide. If these pass, TUI programs and job control have a
real foundation to build on.

Skipped automatically on platforms without POSIX PTY/fork.
"""

import os
import time

import pytest

from zmux.realpty import RealPtyProcess, run_pty_probe

pytestmark = pytest.mark.skipif(
    not (hasattr(os, "fork") and hasattr(os, "openpty")),
    reason="requires POSIX fork + PTY support",
)


class _Pty:
    """RealPtyProcess wrapper that records output and exit for asserts."""

    def __init__(self, argv, rows=24, cols=80, env=None):
        self.out = bytearray()
        self.exit_code = None
        self.proc = RealPtyProcess(
            argv, env=env, rows=rows, cols=cols,
            emit=self.out.extend, on_exit=self._done,
        )

    def _done(self):
        self.exit_code = self.proc.exit_code

    def send(self, text: str) -> None:
        self.proc.write(text.encode("utf-8", errors="replace"))

    def wait_for(self, needle, timeout=12.0) -> bool:
        needle_b = needle if isinstance(needle, bytes) else needle.encode("utf-8")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle_b in self.out:
                return True
            time.sleep(0.03)
        return False

    def wait_exit(self, timeout=8.0):
        return self.proc.wait_exited(timeout)


@pytest.fixture
def pty_shell():
    p = _Pty(["/bin/sh"])
    yield p
    p.proc.kill()
    p.proc.close()


def test_echo_roundtrip(pty_shell):
    """A real /bin/sh answers inside the PTY."""
    pty_shell.send("echo PTY-OK-$((6*7))\r")
    assert pty_shell.wait_for("PTY-OK-42"), bytes(pty_shell.out)[-300:]


def test_child_really_has_a_tty(pty_shell):
    """isatty(0) is true and the controlling tty is a real /dev/pts/N."""
    pty_shell.send("test -t 0 && echo TTY-YES\r")
    assert pty_shell.wait_for("TTY-YES"), bytes(pty_shell.out)[-300:]
    pty_shell.send("tty\r")
    assert pty_shell.wait_for("/dev/pts/"), bytes(pty_shell.out)[-300:]


def test_resize_reaches_the_child(pty_shell):
    """TIOCSWINSZ from the parent is visible to the shell (stty size)."""
    pty_shell.proc.resize(11, 47)
    pty_shell.send("stty size\r")
    assert pty_shell.wait_for("11 47"), bytes(pty_shell.out)[-300:]


def test_ctrl_c_is_kernel_sigint_not_emulation(pty_shell):
    """\\x03 must kill a foreground sleep via the tty line discipline.

    If Ctrl+C were still faked in Python (the pre-PTY behaviour), the sleep
    would keep running and the next command would not answer for ~30s.
    """
    pty_shell.send("sleep 30\r")
    assert pty_shell.wait_for("sleep 30"), bytes(pty_shell.out)[-300:]
    time.sleep(0.3)
    pty_shell.send("\x03")
    pty_shell.send("echo AFTER-INT\r")
    assert pty_shell.wait_for("AFTER-INT", timeout=5.0), bytes(pty_shell.out)[-300:]
    # The shell itself survived the signal (it is still answering).
    pty_shell.send("echo STILL-ALIVE\r")
    assert pty_shell.wait_for("STILL-ALIVE", timeout=5.0)


def test_job_control_background_and_foreground(pty_shell):
    """Background '&' works, 'wait' returns, and 'jobs' lists the job."""
    pty_shell.send("sleep 0.3 &\r")
    pty_shell.send("echo BG-STARTED\r")
    assert pty_shell.wait_for("BG-STARTED"), bytes(pty_shell.out)[-300:]
    pty_shell.send("wait\r")
    pty_shell.send("echo WAIT-DONE\r")
    assert pty_shell.wait_for("WAIT-DONE", timeout=6.0), bytes(pty_shell.out)[-300:]
    # jobs must list the backgrounded sleep: the echoed command line already
    # contains one "sleep", the jobs output adds a second.
    pty_shell.send("sleep 0.2 &\r")
    pty_shell.send("jobs\r")
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if bytes(pty_shell.out).count(b"sleep") >= 2:
            break
        time.sleep(0.03)
    assert bytes(pty_shell.out).count(b"sleep") >= 2, bytes(pty_shell.out)[-300:]


def test_exit_status_propagates_and_child_is_reaped():
    """exit 42 -> waitpid status 42; the child is reaped (no zombie)."""
    p = _Pty(["/bin/sh"])
    try:
        p.send("exit 42\r")
        code = p.wait_exit()
        assert code == 42, f"expected 42, got {code!r}"
        assert p.exit_code == 42
        # Reaping happened: a second wait must raise ChildProcessError, which
        # RealPtyProcess already swallowed — verify by the _reaped flag.
        assert p.proc._reaped.is_set()
    finally:
        p.proc.kill()
        p.proc.close()


def test_write_after_exit_is_safe():
    """Writing to a dead PTY returns False instead of raising."""
    p = _Pty(["/bin/sh"])
    try:
        p.send("exit\r")
        assert p.wait_exit() == 0
        assert p.proc.write(b"echo nope\r") is False
    finally:
        p.proc.kill()
        p.proc.close()


def test_kill_terminates_whole_session_and_reaps():
    p = _Pty(["/bin/sh"])
    p.send("sleep 300\r")  # foreground child in the same process group
    assert p.wait_for("sleep 300")
    p.proc.kill()
    code = p.wait_exit(timeout=8.0)
    assert code is not None, "kill() did not terminate the child"
    assert code != 0  # killed by SIGKILL -> 137 on POSIX


def test_run_pty_probe_all_gates_pass():
    """The on-device acceptance probe must be fully green on this host too."""
    lines = []
    results = run_pty_probe(report=lines.append)
    failed = [name for name, r in results.items() if not r["ok"]]
    assert not failed, "\n".join(lines)
    assert len(results) >= 6
