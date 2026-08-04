"""Interactive terminal session — Alpine PTY shell plus legacy app-control console.

Two terminal personalities exist, but only Alpine is the product shell:

- **Legacy app-control console** (``zmux:~$``): the original Python-native
  line discipline. It remains for migration/tests/internal diagnostics while
  cleanup phases retire host-side package and REPL behavior.
- **Alpine shell (real PTY)**: ``linux`` (bare) — or automatic startup —
  spawns a genuine PTY whose child is ``proot -> /bin/sh -l`` inside the
  Alpine rootfs. From that moment the *kernel* does echo, backspace,
  Ctrl+C (SIGINT to the foreground process group), job control and
  ``isatty()``; this class is only a byte pump between the WebSocket and
  ``/dev/ptmx``. ``vim``/``htop``/``less``/``tmux`` work here. ``exit`` (or
  Ctrl+D) ends the shell; production auto-reopens Alpine. Any legacy detach
  control is compatibility-only and should not be presented as normal UX.
  Ctrl+B is deliberately not reserved — vim uses it for page-up.

Interactivity (legacy app-control console)
----------------------------
Commands run on a dedicated worker thread so the input path stays live:

- **Ctrl+C** sets the cooperative interrupt flag, SIGINTs any in-flight
  subprocess pipeline (escalating to SIGKILL), and injects
  ``KeyboardInterrupt`` into the worker thread via
  ``PyThreadState_SetAsyncExc``. (In PTY mode Ctrl+C is the kernel's job —
  raw ``\\x03`` goes straight to the line discipline.)
- **stdin**: while a command runs, typed lines queue as stdin — ``input()``
  works. Lines still queued when the command finishes become type-ahead
  commands, matching real terminal semantics.
- **history**: Up/Down (``ESC [ A`` / ``ESC [ B``) recall submitted lines.
"""
from __future__ import annotations

import codeop
import contextlib
import os
import platform
import queue
import sys
import threading
from typing import Optional

from zmux import crash
from zmux.paths import HOME_DIR, RC_FILENAME, display_path, read_rc_lines, seed_examples
from zmux.python_shell import PythonShell


ALPINE_PRODUCT_SHELL = True
"""WebSocket sessions should present Alpine in a real PTY as the product shell."""

LEGACY_APP_CONTROL_CONSOLE = True
"""The Python app-control console remains only as a migration/internal fallback."""

LEGACY_PTY_TOGGLE_WARNING = (
    b"[legacy compatibility: app-control console; use the Alpine shell for normal work]\r\n"
)
"""Warning shown when a compatibility toggle detaches from the Alpine PTY."""

_SENTINEL_COMPILE_ERROR = object()
_CSI_FINAL_BYTES = frozenset(
    chr(c) for c in range(0x40, 0x7F)  # @A–Z[\]^_`a–z{|}~
)
_REPL_ENTER = {"python", "python3"}
_REPL_EXIT = {"exit", "quit", "exit()", "quit()"}
#: Bare words that open the real Alpine PTY shell from the legacy app-control console.
_ALPINE_ENTER = {"linux", "alpine"}

# Private terminal control emitted by the generated Alpine
# `zmux-setup-storage` wrapper. It is consumed before xterm sees it, then the
# trusted Android bridge writes a normal terminal result back to this session.
_STORAGE_SETUP_OSC = b"\x1b]777;zmux-setup-storage\x07"


class _QueueInput:
    """File-like stdin backed by the session's queue (for ``input()``).

    Blocks in short slices so a Ctrl+C (or session stop) unblocks the read
    instead of hanging the worker thread forever.
    """

    def __init__(self, session: "PTYTerminalSession") -> None:
        self._session = session

    def readline(self, _size: int = -1) -> str:
        session = self._session
        # Push any partial line (typically the input() prompt itself, which
        # has no trailing newline) before blocking. Without this the user is
        # asked a question they cannot see and the terminal looks frozen.
        stream = getattr(sys.stdout, "flush", None)
        if stream is not None:
            with contextlib.suppress(Exception):
                sys.stdout.flush()
        while True:
            if session.shell._interrupt.is_set():
                raise KeyboardInterrupt
            if not session.is_running:
                raise EOFError
            try:
                return session._stdin_queue.get(timeout=0.05) + "\n"
            except queue.Empty:
                continue

    def isatty(self) -> bool:
        return True


def _inject_keyboard_interrupt(thread: Optional[threading.Thread]) -> bool:
    """Raise KeyboardInterrupt asynchronously inside ``thread``.

    Same mechanism debuggers use to break into running code. Unavailable
    ctypes (or a dead thread) simply returns False; the cooperative flag in
    :meth:`PythonShell.interrupt` still covers stdin/subprocess waits.
    """
    if thread is None or not thread.is_alive():
        return False
    try:
        import ctypes
    except ImportError:
        return False
    result = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread.ident or 0), ctypes.py_object(KeyboardInterrupt)
    )
    if result > 1:  # pragma: no cover - corrupted state, undo per CPython docs
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident or 0), None)
        return False
    return result == 1


class PTYTerminalSession:
    """Persistent, Python-native interactive terminal session.

    The public name is retained for compatibility with the websocket server,
    but this is a virtual terminal rather than a Unix PTY.
    """

    HISTORY_LIMIT = 500

    def __init__(self, ws_server, emit=None):
        self.ws_server = ws_server
        #: Where output goes. The session manager injects a callback that
        #: forwards to the websocket only while this session is on screen, so
        #: background sessions keep running without corrupting the display.
        #: Defaults to broadcasting directly (single-session / test use).
        self._emit_output = emit if emit is not None else ws_server.broadcast
        #: True when this session owns the connection outright and may claim
        #: the websocket's input callbacks. Under the session manager, routing
        #: is the manager's job and a background session must never steal it.
        #: (Tracked explicitly: comparing bound methods with `is` is always
        #: False, since each attribute access builds a new method object.)
        self._owns_ws = emit is None
        self.shell = PythonShell()
        self.is_running = False
        self.process = None  # Compatibility: no shell process is spawned.
        self.lock = threading.RLock()
        self.buffer_lock = threading.Lock()
        self.scrollback_buffer = bytearray()
        #: Per-session scrollback cap. 32 KiB was enough to lose long outputs
        #: mid-listing; 1 MiB per session bounds the worst case to
        #: MAX_SESSIONS × 1 MiB = 8 MiB of RAM, which is affordable even on
        #: Android Go while keeping a full `ls -R` of a large tree visible.
        self.scrollback_max_size = 1 << 20

        self._line_buffer = ""
        self._python_lines: list[str] = []
        #: Real PTY child (proot -> Alpine /bin/sh) when in shell mode.
        self.pty = None  # RealPtyProcess | None
        #: Last known terminal geometry (used when spawning the PTY and on
        #: resize passthrough).
        self._rows = 24
        self._cols = 80
        #: True when the last emitted byte left the cursor at the start of a
        #: fresh line. The prompt refuses to be pasted onto the tail of a
        #: command's output that did not end with a newline, so after
        #: `print(1, end="")` the next prompt starts on its own line instead
        #: of rendering as `1zmux:~$ `.
        self._at_line_start = True
        self._mode = "shell"  # "shell" | "repl"
        self._history: list[str] = []
        self._history_index: Optional[int] = None
        self._history_stash = ""
        self._esc_state = ""  # "" | "esc" | "csi"
        # Before the rootfs exists, this is a setup gate rather than a hidden
        # Python shell. It accepts only linux-setup and then enters Alpine.
        self._awaiting_alpine = False
        self._start_alpine_after_command = False
        # The extensive legacy executor tests intentionally exercise the old
        # internal command runner without an Alpine fixture. Production never
        # exposes that runner as a user shell.
        self._setup_gate_enabled = os.environ.get("ZMUX_TEST") != "1"
        # PTY output can split an OSC sequence across read() calls. Retain only
        # the possible prefix and forward every other byte unchanged.
        self._pty_control_tail = b""
        self._storage_setup_pending = threading.Event()

        self._command_queue: queue.Queue = queue.Queue()
        self._stdin_queue: queue.Queue[str] = queue.Queue()
        self._stdin = _QueueInput(self)
        self._busy = threading.Event()
        self._exec_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            # Only a session that owns the websocket outright registers here.
            # Under the session manager, routing belongs to the manager, so
            # a newly created background session must not steal input.
            if self._owns_ws:
                self.ws_server.register_callbacks(on_data=self.write_input, on_resize=self.resize)
            self._exec_thread = threading.Thread(
                target=self._exec_loop, daemon=True, name="ZMUX-Terminal-Exec"
            )
            self._exec_thread.start()
            # Alpine is the product shell. The embedded Python runtime remains
            # an implementation detail for the Android bridge, never a second
            # user-facing pseudo-shell. Keep the private rc hook for existing
            # installs and migration logic; it is never presented as a shell.
            self._run_rc()
            if self._auto_start_alpine():
                self._enter_linux_pty()
            else:
                if self._setup_gate_enabled:
                    self._awaiting_alpine = True
                    self._emit(
                        b"ZMUX needs its Alpine Linux environment before a terminal can open.\r\n"
                        b"Type `linux-setup` to download and verify Alpine, then ZMUX opens it automatically.\r\n"
                    )
                    self._emit_setup_prompt()
                else:  # test-only legacy executor coverage
                    self._emit_prompt()

    def _run_rc(self) -> None:
        """Execute the legacy host-side ``~/.zmuxrc`` hook, if present.

        This exists for migration and compatibility with the legacy app-control
        console. It is not the Alpine shell startup file; user-facing shell
        customisation should go in Alpine ``~/.profile``. Failures are reported
        but never prevent the terminal from starting.
        """
        lines = read_rc_lines(HOME_DIR)
        if not lines:
            return
        self.shell.output_sink = self._emit
        try:
            for line in lines:
                try:
                    result = self.shell.execute(line)
                except Exception as error:  # a bad rc must not kill startup
                    self._emit(f"{RC_FILENAME}: {error}\r\n".encode("utf-8", errors="replace"))
                    continue
                streamed = result.get("streamed", ())
                pending = "".join(
                    result.get(name, "") for name in ("stdout", "stderr")
                    if name not in streamed
                )
                if pending:
                    self._emit(pending.replace("\n", "\r\n").encode("utf-8", errors="replace"))
        finally:
            self.shell.output_sink = None

    def stop(self) -> None:
        with self.lock:
            self.is_running = False
            self._line_buffer = ""
            self._python_lines.clear()
            pty, self.pty = self.pty, None
            if pty is not None:
                pty.kill()
            self.shell.interrupt()
            self._command_queue.put(None)  # poison pill for the worker

    def resize(self, cols: int, rows: int) -> None:
        # Keep the real child PTY sized to the viewport (TIOCSWINSZ). The
        # shell wakes on SIGWINCH and re-lays-out; TUI apps follow.
        if cols > 0:
            self._cols = cols
        if rows > 0:
            self._rows = rows
        pty = self.pty
        if pty is not None:
            pty.resize(self._rows, self._cols)
            return
        # Host console: track the width for rendering (e.g. Rich tracebacks).
        if cols > 0:
            self.shell.width = max(20, cols)

    # ------------------------------------------------------------- output
    def get_scrollback(self) -> bytes:
        with self.buffer_lock:
            return bytes(self.scrollback_buffer)

    @staticmethod
    def _possible_control_prefix(data: bytes) -> int:
        """Length of the longest suffix that may start the storage OSC."""
        limit = min(len(data), len(_STORAGE_SETUP_OSC) - 1)
        for size in range(limit, 0, -1):
            if data[-size:] == _STORAGE_SETUP_OSC[:size]:
                return size
        return 0

    def _filter_pty_controls(self, data: bytes) -> tuple[bytes, int]:
        """Remove complete private OSC controls while preserving normal bytes."""
        pending = self._pty_control_tail + data
        self._pty_control_tail = b""
        out = bytearray()
        controls = 0
        while pending:
            at = pending.find(_STORAGE_SETUP_OSC)
            if at >= 0:
                out.extend(pending[:at])
                pending = pending[at + len(_STORAGE_SETUP_OSC):]
                controls += 1
                continue
            keep = self._possible_control_prefix(pending)
            if keep:
                out.extend(pending[:-keep])
                self._pty_control_tail = pending[-keep:]
            else:
                out.extend(pending)
            break
        return bytes(out), controls

    def _request_storage_setup(self) -> None:
        """Run Android storage setup outside the PTY reader thread."""
        if self._storage_setup_pending.is_set():
            return
        self._storage_setup_pending.set()

        def worker() -> None:
            try:
                from zmux import storage
                self._emit(b"\r\n[ZMUX] Requesting Android storage access...\r\n")
                result = storage.setup()
                text = storage.format_setup(result)
                self._emit((text + "\n").replace("\n", "\r\n").encode("utf-8", errors="replace"))
            except Exception as error:
                self._emit(f"\r\n[ZMUX] Storage setup failed: {error}\r\n".encode("utf-8", errors="replace"))
            finally:
                self._storage_setup_pending.clear()

        threading.Thread(target=worker, daemon=True, name="ZMUX-Storage-Setup").start()

    def _emit(self, data: bytes) -> None:
        data, storage_requests = self._filter_pty_controls(data)
        for _ in range(storage_requests):
            self._request_storage_setup()
        if not data:
            return
        # Scrollback is always recorded, even when this session is in the
        # background — that is what makes switching back able to repaint.
        with self.buffer_lock:
            self.scrollback_buffer.extend(data)
            if len(self.scrollback_buffer) > self.scrollback_max_size:
                del self.scrollback_buffer[: -self.scrollback_max_size]
        if data:
            if b"\x1b[2J" in data or data.endswith(b"\x1b[H"):
                # A clear-screen/home sequence leaves the cursor at the top
                # left. Treat that as a fresh line start so the next prompt
                # renders on row 0 instead of being pushed down one row by
                # an extra CRLF (the "prompt bablas after clear" bug).
                self._at_line_start = True
            elif data.endswith(b"\n"):
                self._at_line_start = True
            elif not data.endswith(b"\r"):
                self._at_line_start = False
        self._emit_output(data)

    def _prompt(self) -> str:
        if self._mode == "repl":
            return ">>> "
        return f"zmux:{display_path(self.shell.cwd)}$ "

    def _emit_prompt(self) -> None:
        # Never paste the prompt onto a command's unterminated output tail.
        if not self._at_line_start:
            self._emit(b"\r\n")
        self._emit(self._prompt().encode("utf-8"))

    def _emit_setup_prompt(self) -> None:
        if not self._at_line_start:
            self._emit(b"\r\n")
        self._emit(b"alpine-setup> ")

    # -------------------------------------------------------------- input
    def write_input(self, data: bytes) -> None:
        """Process real keyboard input.

        In PTY mode every byte goes straight to the master — echo, backspace,
        Ctrl+C and job control are the kernel's line discipline, not ours. In
        host-console mode, queue completed lines/keystrokes as before.
        """
        with self.lock:
            if not self.is_running:
                return
            pty = self.pty
            if pty is not None:
                pty.write(data)
                return
            text = data.decode("utf-8", errors="replace")
            for char in text:
                self._handle_char(char)

    def _handle_char(self, char: str) -> None:
        # Escape-sequence parser first: arrow keys arrive as ESC [ A/B ...
        if self._esc_state or char == "\x1b":
            self._handle_escape_char(char)
            return
        if char == "\x03":  # Ctrl+C
            self._interrupt_running()
        elif char == "\x04":  # Ctrl+D
            if self._mode == "repl" and not self._line_buffer and not self._busy.is_set():
                self._leave_repl()
        elif char in ("\x7f", "\x08"):
            if self._line_buffer:
                self._line_buffer = self._line_buffer[:-1]
                self._emit(b"\b \b")
        elif char in ("\r", "\n"):
            self._emit(b"\r\n")
            self._submit_line(self._line_buffer)
            self._line_buffer = ""
        elif char >= " ":
            self._line_buffer += char
            self._emit(char.encode("utf-8"))

    def _handle_escape_char(self, char: str) -> None:
        if not self._esc_state and char == "\x1b":
            self._esc_state = "esc"
            return
        if self._esc_state == "esc":
            # CSI (ESC [ …) is the xterm.js default; SS3 (ESC O …) covers
            # application-cursor mode — arrows arrive in both dialects.
            self._esc_state = {"[": "csi", "O": "ss3"}.get(char, "")
            return
        if self._esc_state == "ss3":
            if char == "A":
                self._history_step(-1)
            elif char == "B":
                self._history_step(+1)
            self._esc_state = ""
            return
        # csi: swallow until a final byte arrives
        if char in _CSI_FINAL_BYTES:
            if char == "A":
                self._history_step(-1)
            elif char == "B":
                self._history_step(+1)
            self._esc_state = ""

    # ------------------------------------------------------------ history
    def _history_step(self, direction: int) -> None:
        if self._busy.is_set() or not self._history:
            return
        if self._history_index is None:
            if direction > 0:
                return
            self._history_stash = self._line_buffer
            self._history_index = len(self._history)
        new_index = self._history_index + direction
        if new_index >= len(self._history):
            self._history_index = None
            self._replace_line(self._history_stash)
            return
        self._history_index = max(0, new_index)
        self._replace_line(self._history[self._history_index])

    def _replace_line(self, text: str) -> None:
        self._line_buffer = text
        self._emit(b"\r\x1b[K" + text.encode("utf-8"))

    def _history_record(self, line: str) -> None:
        if line and (not self._history or self._history[-1] != line):
            self._history.append(line)
            del self._history[: -self.HISTORY_LIMIT]
        self._history_index = None

    # --------------------------------------------------------- submission
    def _submit_line(self, line: str) -> None:
        if self._busy.is_set():
            # A command is running: the line is stdin for it (or type-ahead).
            self._stdin_queue.put(line)
            return
        stripped = line.strip()
        if not stripped:
            if self._python_lines:
                # A blank line closes an open compound block, exactly like
                # the CPython REPL — hand it to the worker to finish the block.
                self._command_queue.put((line, self._mode))
            else:
                self._emit_prompt()
            return
        self._history_record(stripped)
        if self._awaiting_alpine:
            if stripped == "linux-setup":
                self._command_queue.put((line, "shell"))
            else:
                self._emit(b"Only `linux-setup` is available until Alpine is ready.\r\n")
                self._emit_setup_prompt()
            return
        if self._mode == "shell" and stripped in _REPL_ENTER:
            self._enter_repl()
            return
        if self._mode == "shell" and not self._busy.is_set() and stripped in _ALPINE_ENTER:
            self._enter_linux_pty()
            return
        if self._mode == "repl" and stripped in _REPL_EXIT:
            self._leave_repl()
            return
        self._command_queue.put((line, self._mode))

    def _enter_repl(self) -> None:
        self._mode = "repl"
        self._emit(
            f"Python {platform.python_version()} (embedded ZMUX runtime)\r\n"
            'Type "exit()" or Ctrl+D to return to the shell\r\n'.encode("utf-8")
        )
        self._emit_prompt()

    def _leave_repl(self) -> None:
        self._mode = "shell"
        self._python_lines.clear()
        self._emit_prompt()

    # --------------------------------------------------- real-PTY shell mode
    def in_pty(self) -> bool:
        """True while the Alpine PTY shell owns the screen."""
        return self.pty is not None

    def _auto_start_alpine(self) -> bool:
        """Return whether the Alpine product environment is ready.

        A legacy host-console override used to live here. Keeping that second
        shell exposed made normal POSIX commands behave like Python and was
        the root of much user confusion; ZMUX now has one user-facing shell.
        """
        from zmux import linuxenv
        return linuxenv.is_installed() and linuxenv.proot_binary() is not None

    def _enter_linux_pty(self) -> None:
        """Spawn the real Alpine shell: PTY -> proot -> /bin/sh -l.

        Once alive, every input byte goes raw to the master; the kernel does
        the terminal work. `exit`/Ctrl+D ends the shell and returns here;
        Ctrl+B detaches (terminating the Alpine session) and returns here.
        """
        from zmux import linuxenv
        from zmux.realpty import RealPtyProcess
        with self.lock:
            if not self.is_running or self.pty is not None or self._busy.is_set():
                return
            if not linuxenv.is_installed():
                self._emit(
                    b"zmux: Alpine environment is not installed.\r\n"
                    b"  Run `linux-setup` once (downloads ~4 MiB, SHA-512 verified),\r\n"
                    b"  then `linux` opens the real Alpine shell.\r\n"
                )
                self._emit_prompt()
                return
            try:
                argv = linuxenv.build_interactive_argv(self.shell.cwd)
                env = linuxenv.interactive_env()
            except RuntimeError as error:
                self._emit(f"zmux: {error}\r\n".encode("utf-8", errors="replace"))
                self._emit_prompt()
                return
            self._mode = "shell"
            self._emit(b"\r\n[ZMUX Alpine Linux - real PTY]\r\n")
            try:
                holder: dict = {}
                def _on_pty_exit():
                    # Bind the exact instance: a later re-entry must never let
                    # a stale reader's on_exit clobber the newer pty.
                    self._pty_exited(holder["pty"])
                pty = RealPtyProcess(
                    argv,
                    env=env,
                    rows=self._rows,
                    cols=self._cols,
                    emit=self._emit,
                    on_exit=_on_pty_exit,
                )
                holder["pty"] = pty
            except RuntimeError as error:
                self._emit(f"zmux: {error}\r\n".encode("utf-8", errors="replace"))
                self._emit_prompt()
                return
            self.pty = pty
            # Make sure the ZMUX host-tool wrappers exist inside the rootfs so
            # `gates`/`zpip`/... are not silently "command not found" in Alpine.
            try:
                linuxenv.install_guest_wrappers()
            except Exception:
                pass

    def _pty_exited(self, pty) -> None:
        """Reader-thread callback: the Alpine shell process ended.

        ``pty`` is the exact instance that exited. If the session already
        moved on (detach + re-entry spawned a newer pty), this stale exit
        must not clear the newer one.
        """
        with self.lock:
            if self.pty is not pty:
                return
            self.pty = None
        code = pty.exit_code
        self._emit(
            f"\r\n[Alpine shell exited (code {code})]\r\n".encode("utf-8", errors="replace")
        )
        # A terminal emulator does not drop users into an unrelated interpreter
        # after `exit`. Keep the one user-facing contract: if the app/session
        # remains alive, replace the exited Alpine shell with a fresh PTY.
        if self.is_running and self._auto_start_alpine():
            self._enter_linux_pty()

    def _leave_linux_pty(self, reason: str = "detach") -> None:
        """Terminate the Alpine PTY session and enter the legacy app-control console."""
        with self.lock:
            pty = self.pty
            self.pty = None
        if pty is not None:
            pty.kill()
        if reason == "detach":
            self._emit(b"\r\n[detached from Alpine shell - ZMUX host console]\r\n")
        else:
            self._emit(b"\r\n[Alpine shell stopped]\r\n")
        self._emit_prompt()

    def toggle_pty(self) -> None:
        """Compatibility toggle between Alpine PTY and legacy app-control console."""
        with self.lock:
            if not self.is_running:
                return
            if self.pty is not None:
                self._leave_linux_pty("detach")
            else:
                self._enter_linux_pty()

    # ----------------------------------------------------------- interrupt
    def _interrupt_running(self) -> None:
        self._line_buffer = ""
        self._python_lines.clear()
        self._emit(b"^C\r\n")
        if self._busy.is_set():
            self.shell.interrupt()  # flag + SIGINT pipeline, escalating
            # Only inject into pure-Python execution. When a subprocess
            # pipeline owns the wait, signals above cancel it deterministically
            # and injecting here could eat the pipeline's result instead.
            if not self.shell.has_running_processes():
                _inject_keyboard_interrupt(self._exec_thread)
        else:
            self._emit_prompt()

    # ------------------------------------------------------------- worker
    def _shell_commands(self) -> set:
        # python/python3 with arguments (`python file.py`, `python -c ...`)
        # route to the real script runner; the bare-word REPL entry was
        # already intercepted in _submit_line and never reaches the worker.
        # Known TUI names are routed to the command executor so they render
        # the "needs a real TTY" hint instead of a Python NameError.
        return set(self.shell.commands) | {
            "pip", "zpip", "help", "zmux-info", "zmux-setup-storage", "python", "python3",
            "git", "linux", "alpine", "linux-setup", "gates", "zmux-pty-probe",
        } | set(self.shell.KNOWN_TUI_COMMANDS)

    def _exec_loop(self) -> None:
        """Single worker: executes queued command lines one at a time."""
        while True:
            try:
                item = self._command_queue.get()
            except KeyboardInterrupt:
                # Async injection arriving at idle (between commands) is
                # normal — swallow it, never let the worker thread die here.
                continue
            if item is None or not self.is_running:
                return
            line, mode = item
            # Snapshot BEFORE marking busy: a Ctrl+C landing afterwards bumps
            # the epoch, so the tagged clear below refuses to wipe it — the
            # spawn-race and pipeline checks rely on the flag surviving.
            epoch = self.shell._interrupt_epoch
            self._busy.set()
            self.shell.clear_interrupt(epoch=epoch)
            try:
                self._run_line(line, mode)
            except KeyboardInterrupt:
                # A Ctrl+C injection may land just after the command finished
                # (async delivery is racy by nature) — it must never kill the
                # worker thread, just render like an interrupted command.
                self._emit(b"KeyboardInterrupt\r\n")
                self.shell._interrupt.set()
                self._emit_prompt()
            except Exception as error:  # never let the worker die
                # Keeping the worker alive is right, but silently discarding
                # the traceback made real bugs unreportable — persist it.
                crash.record("terminal-exec", type(error), error, error.__traceback__)
                self._emit(f"[session error: {error}]\r\n".encode("utf-8", errors="replace"))
                self._emit_prompt()
            finally:
                self._finish_command()

    def _run_line(self, line: str, mode: str) -> None:
        if mode == "repl":
            self._run_python_line(line)
            return
        first = line.lstrip().split(maxsplit=1)[0] if line.strip() else ""
        if first in self._shell_commands() or self.shell._is_external_command(first):
            self._execute_and_render(line)
        else:
            self._run_python_line(line)  # shell mode Python escape hatch

    def _run_python_line(self, line: str) -> None:
        candidate = "\n".join([*self._python_lines, line])
        try:
            pending = codeop.compile_command(candidate, "<zmux>", "exec")
        except (SyntaxError, OverflowError, ValueError):
            pending = _SENTINEL_COMPILE_ERROR  # execute to render the real error
        if pending is None:
            self._python_lines.append(line)
            self._emit(b"... ")
            self._busy.clear()  # awaiting more input, not running
            return
        self._execute_and_render(candidate, force_python=True)
        self._python_lines.clear()

    def _execute_and_render(self, source: str, force_python: bool = False) -> None:
        """Run one line with output streaming live to the websocket.

        ``output_sink`` makes the shell push text as it is produced, so a
        progressive command is visible while it runs and — critically — an
        ``input()`` prompt reaches the screen *before* the read blocks.
        The result dict still carries the full text; it is deliberately not
        re-emitted here or every line would appear twice.
        """
        self.shell.stdin_provider = self._stdin
        self.shell.output_sink = self._emit
        try:
            result = self.shell.execute(source, force_python=force_python)
        finally:
            self.shell.stdin_provider = None
            self.shell.output_sink = None
        # Built-in commands (ls, echo, cd...) and zpip return their text
        # without touching the sink, so they still need rendering here. The
        # `streamed` tuple names what already reached the screen.
        streamed = result.get("streamed", ())
        pending = "".join(
            result.get(name, "") for name in ("stdout", "stderr") if name not in streamed
        )
        if pending:
            self._emit(pending.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8", errors="replace"))
        if self._awaiting_alpine:
            from zmux import linuxenv
            if linuxenv.is_installed() and linuxenv.proot_binary() is not None:
                self._awaiting_alpine = False
                # _enter_linux_pty deliberately refuses while the command is
                # busy; defer it to _finish_command after linux-setup exits.
                self._start_alpine_after_command = True
            else:
                self._emit_setup_prompt()
        else:
            self._emit_prompt()

    def _finish_command(self) -> None:
        interrupted = self.shell._interrupt.is_set()
        self._busy.clear()
        if interrupted:
            # After Ctrl+C, discard type-ahead instead of executing stale lines.
            self._drain(self._stdin_queue)
            self.shell.clear_interrupt()
            return
        if self._start_alpine_after_command:
            self._start_alpine_after_command = False
            self._enter_linux_pty()
            return
        # Real-terminal type-ahead: lines typed while the command ran become
        # the next commands, in order.
        for leftover in self._drain(self._stdin_queue):
            self._submit_line(leftover)

    @staticmethod
    def _drain(source: "queue.Queue[str]") -> list:
        items = []
        while True:
            try:
                items.append(source.get_nowait())
            except queue.Empty:
                return items


_pty_session: Optional[PTYTerminalSession] = None


def get_pty_session(ws_server) -> PTYTerminalSession:
    """Return the single legacy session.

    Superseded by :mod:`zmux.sessions`, which supports several sessions and
    is what the server and websocket layer now use. Kept for callers that
    only ever want one terminal (and for existing tests).
    """
    global _pty_session
    if _pty_session is None:
        _pty_session = PTYTerminalSession(ws_server)
    return _pty_session
