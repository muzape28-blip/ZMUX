"""Real Unix PTY child processes — the foundation of ZMUX's shell mode.

This module is the honest replacement for the hand-rolled line discipline in
:mod:`zmux.pty_session`. Once a session is in PTY mode, the *kernel* does the
terminal work (echo, backspace, Ctrl+C -> SIGINT to the foreground process
group, job control, ``isatty()``), and this module is only a byte pump:

    xterm.js <-> WebSocket <-> RealPtyProcess.read/write <-> /dev/ptmx
                                                                    |
                                                              (slave)
                                                              shell child

Child wiring (classic terminal-emulator pattern, same as Termux and every
Unix terminal emulator):

1. ``os.openpty()``            -> master/slave pair
2. ``os.fork()``               -> child continues below
3. child: ``setsid()``         -> new session; child becomes session leader
4. child: ``TIOCSCTTY``        -> the pty slave becomes the controlling tty
5. child: ``dup2(slave, 0..2)``-> stdio is the terminal
6. child: ``execve(argv)``     -> shell (or proot -> guest shell)

Between fork and exec the child runs **direct libc syscalls only** (no Python
heap/thread work), mirroring the codebase invariant that arbitrary Python in
a forked child deadlocks Bionic libc on ARMv7; ``os.fork`` itself is the same
mechanism ``subprocess.Popen`` already uses on every ZMUX command today.

``run_pty_probe`` is the on-device acceptance probe (``zmux-pty-probe``):
nothing is mocked, every check forks a real child on a real PTY.
"""

from __future__ import annotations

import fcntl
import os
import signal
import struct
import termios
import threading
import time
from typing import Callable, Dict, List, Optional

#: emit(data: bytes) -> None — every byte read from the master goes here.
EmitFn = Callable[[bytes], None]
#: on_exit() -> None — called after the child has been reaped.
OnExitFn = Callable[[], None]


def set_winsize(master: int, rows: int, cols: int) -> None:
    """Set the PTY window size (TIOCSWINSZ). Raises OSError on failure."""
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    fcntl.ioctl(
        master,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, cols, 0, 0),
    )


def get_winsize(master: int) -> tuple:
    """Read the PTY window size (TIOCGWINSZ). Returns (rows, cols)."""
    packed = fcntl.ioctl(master, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return rows, cols


class RealPtyProcess:
    """A real PTY-backed child process (``fork`` + ``setsid`` + ``TIOCSCTTY``).

    Write bytes with :meth:`write` (raw, no line discipline on our side),
    resize with :meth:`resize`, terminate with :meth:`terminate`/
    :meth:`kill`. Output streams to ``emit`` as it is produced; the child is
    reaped automatically and ``on_exit`` fires once, always from the reader
    thread.
    """

    READ_CHUNK = 65536

    def __init__(
        self,
        argv: List[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        rows: int = 24,
        cols: int = 80,
        emit: Optional[EmitFn] = None,
        on_exit: Optional[OnExitFn] = None,
    ) -> None:
        if not argv:
            raise ValueError("argv must not be empty")
        master, slave = os.openpty()
        try:
            set_winsize(master, rows, cols)
        except OSError:
            pass  # resize before exec is best-effort; TIOCSWINSZ later works
        env = dict(os.environ if env is None else env)
        env.setdefault("TERM", "xterm-256color")
        try:
            pid = os.fork()
        except OSError as error:
            os.close(master)
            os.close(slave)
            raise RuntimeError(f"fork failed (PTY unavailable?): {error}") from error

        if pid == 0:
            # ------------------------------------------------------ child
            # Direct syscalls only until execve: no Python-level work that
            # could hit Bionic/CPython locks in the forked child.
            try:
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                os.close(master)
                if cwd:
                    os.chdir(cwd)
                os.execve(argv[0], argv, env)
            except BaseException:
                pass
            os._exit(127)

        # ---------------------------------------------------------- parent
        os.close(slave)
        self.pid = pid
        self.master = master
        self.argv = list(argv)
        self._emit = emit if emit is not None else (lambda data: None)
        self._on_exit = on_exit
        self._exited = threading.Event()
        self.exit_code: Optional[int] = None
        self._reaped = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name=f"ZMUX-RealPTY-{pid}",
        )
        self._reader.start()

    # ------------------------------------------------------------- output
    def _read_loop(self) -> None:
        try:
            while not self._exited.is_set():
                try:
                    data = os.read(self.master, self.READ_CHUNK)
                except OSError:
                    break  # master closed (terminate/kill) or pty gone
                if not data:
                    break  # EOF: every slave fd is closed -> child gone
                try:
                    self._emit(data)
                except Exception:
                    break  # a dead sink must never wedge the reader
        finally:
            self._exited.set()
            self._reap()
            if self._on_exit is not None:
                try:
                    self._on_exit()
                except Exception:
                    pass

    def _reap(self) -> None:
        """Reap the child. If it somehow survived EOF (a grandchild held the
        slave open), SIGKILL the whole session/process group first."""
        if self._reaped.is_set():
            return
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._reaped.set()
            return
        if pid == 0:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                pid, status = os.waitpid(self.pid, 0)
            except ChildProcessError:
                self._reaped.set()
                return
        self.exit_code = os.waitstatus_to_exitcode(status)
        self._reaped.set()

    # -------------------------------------------------------------- input
    def write(self, data: bytes) -> bool:
        """Write raw bytes to the master. Returns False if the pty is gone."""
        if not data or self._exited.is_set():
            return False
        try:
            os.write(self.master, data)
            return True
        except OSError:
            return False

    def resize(self, rows: int, cols: int) -> None:
        if self._exited.is_set():
            return
        try:
            set_winsize(self.master, rows, cols)
        except OSError:
            pass

    # ---------------------------------------------------------- lifecycle
    def terminate(self) -> None:
        """SIGTERM to the whole session (process group). Reader reaps."""
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def kill(self) -> None:
        """SIGKILL the session and unblock the reader immediately.

        Some PRoot process trees keep a slave descriptor alive briefly after
        their leader dies. Merely signalling the process group then leaves the
        reader blocked in ``os.read`` and makes shutdown/reconnect appear
        flaky. Closing the master is safe after SIGKILL and guarantees the
        reader reaches its reap path.
        """
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            pass
        self.close()

    def close(self) -> None:
        """Close the master. Unblocks a reader stuck in os.read()."""
        try:
            os.close(self.master)
        except OSError:
            pass
        self._exited.set()

    def wait_exited(self, timeout: float = 5.0) -> Optional[int]:
        """Block until the child is reaped. Returns the exit code, or None
        on timeout."""
        if not self._reaped.wait(timeout):
            return None
        return self.exit_code


# ---------------------------------------------------------------------------
# Strict on-device probe ("zmux-pty-probe") — nothing here is mocked
# ---------------------------------------------------------------------------
class _ProbePty:
    """A RealPtyProcess wrapper that records output + exit code for asserts."""

    def __init__(self, argv, rows=24, cols=80, env=None):
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.exit_code: Optional[int] = None
        self.proc = RealPtyProcess(
            argv,
            env=env,
            rows=rows,
            cols=cols,
            emit=self._record,
            on_exit=self._note_exit,
        )

    def _record(self, data: bytes) -> None:
        with self.cond:
            self.buf.extend(data)
            self.cond.notify_all()

    def _note_exit(self) -> None:
        with self.cond:
            self.exit_code = self.proc.exit_code
            self.cond.notify_all()

    def send(self, text: str) -> None:
        self.proc.write(text.encode("utf-8", errors="replace"))

    def wait_for(self, needle, timeout: float = 8.0) -> bool:
        needle_b = needle if isinstance(needle, bytes) else needle.encode("utf-8")
        deadline = time.monotonic() + timeout
        with self.cond:
            while needle_b not in self.buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.cond.wait(remaining)
            return True

    def wait_exit(self, timeout: float = 5.0) -> Optional[int]:
        return self.proc.wait_exited(timeout)


def _probe_openpty() -> dict:
    """pty1: /dev/ptmx opens and TIOCSWINSZ/GWINSZ round-trip."""
    master, slave = os.openpty()
    try:
        set_winsize(master, 17, 53)
        rows, cols = get_winsize(master)
        ok = (rows, cols) == (17, 53)
        return {"ok": ok, "detail": f"openpty ok, winsize {rows}x{cols} round-trip"}
    except OSError as error:
        return {"ok": False, "detail": f"openpty/TIOCSWINSZ failed: {error}"}
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        try:
            os.close(slave)
        except OSError:
            pass


def _probe_echo() -> dict:
    """pty2: a real /bin/sh runs inside the PTY and answers."""
    if not hasattr(os, "fork"):
        return {"ok": False, "detail": "os.fork unavailable in this runtime"}
    p = _ProbePty(["/bin/sh"])
    try:
        p.send("echo PTY-OK-$((6*7))\r")
        if not p.wait_for("PTY-OK-42", timeout=8.0):
            return {"ok": False, "detail": f"no reply; got: {bytes(p.buf)[:200]!r}"}
        p.send("exit\r")
        code = p.wait_exit()
        if code is None:
            p.proc.kill()
            return {"ok": False, "detail": "shell did not exit after 'exit'"}
        return {"ok": True, "detail": f"shell answered, exit={code}"}
    finally:
        p.proc.kill()


def _probe_isatty() -> dict:
    """pty3: the child really has a tty (isatty + controlling tty)."""
    p = _ProbePty(["/bin/sh"])
    try:
        p.send("test -t 0 && echo TTY-YES\r")
        tty_yes = p.wait_for("TTY-YES", timeout=8.0)
        p.send("tty\r")
        pty_path = p.wait_for("/dev/pts/", timeout=8.0)
        detail = "isatty=true, controlling tty=/dev/pts/*" if (tty_yes and pty_path) else \
            f"isatty={tty_yes}, ctl-tty={pty_path}; out: {bytes(p.buf)[:200]!r}"
        p.send("exit\r")
        p.wait_exit()
        return {"ok": bool(tty_yes and pty_path), "detail": detail}
    finally:
        p.proc.kill()


def _probe_resize() -> dict:
    """pty4: TIOCSWINSZ reaches the shell ('stty size' matches)."""
    p = _ProbePty(["/bin/sh"], rows=24, cols=80)
    try:
        p.proc.resize(11, 47)
        p.send("stty size\r")
        if p.wait_for("11 47", timeout=8.0):
            p.send("exit\r")
            p.wait_exit()
            return {"ok": True, "detail": "stty size == 11 47 after TIOCSWINSZ"}
        return {"ok": False, "detail": f"'stty size' mismatch; got: {bytes(p.buf)[:200]!r}"}
    finally:
        p.proc.kill()


def _probe_sigint() -> dict:
    """pty5: Ctrl+C is kernel SIGINT to the foreground pgrp (not emulated).

    A foreground `sleep 30` must die on \\x03, and the shell must accept the
    very next command immediately. If ZMUX were still faking Ctrl+C, either
    the sleep would keep running (echo blocked 30s) or the shell would die.
    """
    p = _ProbePty(["/bin/sh"])
    try:
        p.send("sleep 30\r")
        if not p.wait_for("sleep 30", timeout=8.0):
            return {"ok": False, "detail": "foreground sleep did not start"}
        time.sleep(0.4)
        p.send("\x03")                       # raw Ctrl+C byte -> line discipline
        p.send("echo AFTER-INT\r")
        if not p.wait_for("AFTER-INT", timeout=5.0):
            p.proc.kill()
            return {"ok": False, "detail": "Ctrl+C did not kill foreground sleep"}
        p.send("exit\r")
        p.wait_exit()
        return {"ok": True, "detail": "\\x03 killed foreground sleep; shell alive"}
    finally:
        p.proc.kill()


def _probe_exit_code() -> dict:
    """pty6: exit status propagates and the child is reaped (no zombie)."""
    p = _ProbePty(["/bin/sh"])
    try:
        p.send("exit 42\r")
        code = p.wait_exit()
        if code == 42:
            return {"ok": True, "detail": "exit 42 -> waitpid status 42, reaped"}
        return {"ok": False, "detail": f"expected exit 42, got {code!r}"}
    finally:
        p.proc.kill()


def run_pty_probe(report=None) -> dict:
    """Run every real-PTY gate. Returns {name: {"ok": bool, "detail": str}}.

    ``report`` receives ``[PASS]/[FAIL]`` lines as checks complete (defaults
    to print).
    """
    if report is None:
        report = print
    results: dict = {}

    def gate(name, ok, detail):
        results[name] = {"ok": bool(ok), "detail": str(detail)}
        report(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        return bool(ok)

    checks = [
        ("pty1-openpty", _probe_openpty),
        ("pty2-shell-runs", _probe_echo),
        ("pty3-isatty", _probe_isatty),
        ("pty4-resize", _probe_resize),
        ("pty5-sigint", _probe_sigint),
        ("pty6-exit-code", _probe_exit_code),
    ]
    for name, fn in checks:
        try:
            result = fn()
        except Exception as error:  # a probe crash is a FAIL, not a hang
            result = {"ok": False, "detail": f"probe crashed: {type(error).__name__}: {error}"}
        gate(name, result["ok"], result["detail"])
    return results
