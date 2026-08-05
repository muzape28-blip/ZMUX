"""Legacy host-side Python command interpreter.

This module deliberately never starts ``/system/bin/sh``. Python source is
executed by the embedded p4a CPython runtime and Android utilities, when
needed, are started directly with ``subprocess`` and an absolute executable
path.

The Alpine PTY shell is the product terminal. ``PythonShell`` remains only for
REST ``/api/exec`` compatibility, app-control commands that still need embedded
Python, migration hooks, and tests while the host-console era is retired.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from zmux.env import build_env, build_path
from zmux.paths import HOME_DIR
from zmux.streams import StreamingWriter


LEGACY_COMPATIBILITY_EXECUTOR = True
"""This executor is retained for compatibility; Alpine PTY is the product shell."""

LEGACY_PYTHON_FALLBACK = True
"""Unknown host-console lines may still fall through to Python for compatibility."""

STRICT_HOST_COMMANDS_ENV = "ZMUX_STRICT_HOST_COMMANDS"
"""Opt-in env flag: command-like unknown lines skip Python fallback."""


# --- Optional Rich rendering (DX polish) --------------------------------------
# Rich is a pure-python universal wheel; when installed (e.g. via
# ``zpip install rich``) tracebacks render syntax-highlighted. Everything
# degrades to plain ``traceback.format_exc()`` output when it is absent.
_RICH_UNSET = object()
_rich_impl = _RICH_UNSET


def _get_rich():
    """Import rich lazily, once. Returns (Console, Traceback) or None."""
    global _rich_impl
    if _rich_impl is _RICH_UNSET:
        try:
            from rich.console import Console
            from rich.traceback import Traceback
            _rich_impl = (Console, Traceback)
        except Exception:
            _rich_impl = None
    return _rich_impl


class PythonShell:
    """Legacy persistent Python REPL with real Python filesystem commands.

    ``execute`` returns terminal-style dictionaries for compatibility callers.
    It is not the normal user-facing ZMUX shell; WebSocket sessions should boot
    Alpine in a real PTY. No output is fabricated: output is either produced by
    CPython, Python's filesystem APIs, or a real child process.
    """

    def __init__(self, cwd: Path | None = None):
        self.cwd = Path(cwd or HOME_DIR).resolve()
        self.globals = {"__name__": "__main__", "__package__": None, "__builtins__": __builtins__}
        self.commands: dict[str, Callable[[list[str]], str]] = {
            "ls": self._cmd_ls, "mkdir": self._cmd_mkdir, "rm": self._cmd_rm,
            "cp": self._cmd_cp, "mv": self._cmd_mv, "cat": self._cmd_cat,
            "touch": self._cmd_touch, "echo": self._cmd_echo, "pwd": self._cmd_pwd,
            "cd": self._cmd_cd, "clear": self._cmd_clear, "env": self._cmd_env,
            "which": self._cmd_which, "uname": self._cmd_uname,
        }
        # --- Interactivity / cancellation infrastructure -----------------------
        #: Terminal width reported by the front-end (used by Rich rendering).
        self.width = 80
        #: Optional file-like stdin provider installed while user code runs;
        #: the websocket terminal feeds it so ``input()`` works.
        self.stdin_provider = None
        #: Optional callback taking already-encoded terminal bytes. When set,
        #: command output streams to it as it is produced instead of being
        #: buffered until the command finishes. The returned result dict still
        #: carries the complete text, so REST callers are unaffected.
        self.output_sink = None
        #: Cooperative Ctrl+C: set by :meth:`interrupt`; checked by the stdin
        #: provider and combined with async KeyboardInterrupt injection.
        self._interrupt = threading.Event()
        #: Monotonic counter bumped on every Ctrl+C. Lets the execution loop
        #: distinguish "flag set before this command started" (discardable)
        #: from "flag set for this command" without a busy-state race window.
        self._interrupt_epoch = 0
        #: In-flight pipeline processes, so :meth:`interrupt` can signal them.
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        #: >0 while inside _exec_subprocess (even between spawn and register),
        #: so Ctrl+C classification never misreads a pipeline as pure Python.
        self._subprocess_depth = 0

    # ------------------------------------------------------------------ cancel
    def interrupt(self) -> None:
        """Cooperative Ctrl+C.

        Sets the interrupt flag (stdin providers stop blocking) and forwards
        SIGINT to any in-flight pipeline process group, escalating to SIGKILL.
        Pure-Python runaway code is interrupted by the caller injecting
        ``KeyboardInterrupt`` into the execution thread (see pty_session).
        """
        self._interrupt_epoch += 1
        self._interrupt.set()
        with self._procs_lock:
            victims = [p for p in self._procs if p.poll() is None]
        for proc in victims:
            with contextlib.suppress(ProcessLookupError, OSError):
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                else:
                    proc.send_signal(signal.SIGINT)

        def _escalate() -> None:
            for proc in victims:
                if proc.poll() is not None:
                    continue
                with contextlib.suppress(ProcessLookupError, OSError):
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()

        timer = threading.Timer(1.5, _escalate)
        timer.daemon = True
        timer.start()

    def has_running_processes(self) -> bool:
        """True while subprocess execution owns the worker (pre-spawn counts)."""
        with self._procs_lock:
            if self._subprocess_depth > 0:
                return True
            return any(p.poll() is None for p in self._procs)

    def clear_interrupt(self, epoch: int | None = None) -> None:
        """Reset the Ctrl+C latch.

        With ``epoch``, only clears when no *newer* interrupt arrived since
        that epoch — a Ctrl+C pressed mid-startup must survive loop setup.
        """
        if epoch is None or epoch == self._interrupt_epoch:
            self._interrupt.clear()

    #: Shell operators ZMUX does not implement, mapped to what to tell the
    #: user. Previously these were passed through to the child as ordinary
    #: arguments, which failed *silently*: `/bin/true && touch x` reported
    #: exit 0 and never created x. Failing loudly is strictly better than
    #: pretending to have run something.
    UNSUPPORTED_OPERATORS = (
        ("&&", "'&&' (conditional AND)"),
        ("||", "'||' (conditional OR)"),
        ("2>&1", "'2>&1' (stream merging)"),
        (";", "';' (command sequencing)"),
        ("&", "'&' (background jobs)"),
        ("`", "'`...`' (command substitution)"),
        ("$(", "'$(...)' (command substitution)"),
    )

    def _unsupported_operator(self, parts: list[str], line: str):
        """Return an error result when the line uses an operator we lack.

        Operates on the *tokenised* line so quoted text is never flagged:
        ``echo "a && b"`` is a legitimate single argument. ``$(`` and
        backticks are checked inside tokens too, since those substitute
        without needing whitespace.
        """
        tokens = set(parts)
        for operator, description in self.UNSUPPORTED_OPERATORS:
            hit = operator in tokens
            if not hit and operator in ("`", "$("):
                hit = any(operator in token for token in parts)
            if not hit and operator == ";":
                # `a; b` tokenises as "a;" — a trailing ; fused to a word.
                hit = any(token.endswith(";") for token in parts)
            if hit:
                return self._result(
                    stderr=(
                        f"zmux: {description} is not supported — ZMUX has no shell "
                        f"language.\n"
                        f"  Supported: pipelines (|), redirection (> >> <), quoting.\n"
                        f"  For shell logic use Python instead, e.g. "
                        f"subprocess.run(...) or run commands one per line.\n"
                    ),
                    code=2,
                )
        return None

    def _command_names(self) -> set:
        """Commands accepted by the legacy host-side command dispatcher."""
        return set(self.commands) | {
            "python", "python3", "pip", "zpip", "help", "zmux-info", "zmux-setup-storage",
            "git", "linux", "alpine", "linux-setup", "gates", "zmux-pty-probe",
        }

    def execute(self, line: str, timeout: float | None = None, force_python: bool = False,
                env_extra: dict | None = None) -> dict:
        line = line.strip()
        if not line:
            return self._result()
        if force_python:
            # REPL mode evaluates *everything* as Python — command builtins
            # are intentionally not consulted (`ls` must be a NameError there).
            return self._exec_python(line)
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return self._result(stderr=f"parse error: {exc}\n", code=2)
        if not parts:
            return self._result()
        command = parts[0]
        # Only guard lines that are actually command invocations. Python
        # source legitimately contains ';', '&' and '&&'-like text, and must
        # keep falling through to the interpreter.
        if command in self._command_names() or self._is_external_command(command):
            rejection = self._unsupported_operator(parts, line)
            if rejection is not None:
                return rejection
        # `true && touch x` is shell intent, but `true` is keyword-guarded so
        # it never reaches the check above. Fire the loud shell-operator error
        # for the classic `/bin/true` stand-ins only, and only for operators
        # that are NEVER valid Python — `print("a && b")` and `x = "a && b"`
        # must keep working, and `true & x` (a Python bitwise on a variable
        # named true) is left alone.
        if command in {"true", "false"}:
            for operator, description in self.UNSUPPORTED_OPERATORS:
                if operator == "&":
                    continue
                if operator in ("`", "$("):
                    hit = any(operator in token for token in parts)
                elif operator == ";":
                    hit = ";" in parts or any(token.endswith(";") for token in parts)
                else:
                    hit = operator in parts
                if hit:
                    return self._result(
                        stderr=(
                            f"zmux: {description} is not supported — ZMUX has no shell "
                            f"language.\n"
                            f"  Supported: pipelines (|), redirection (> >> <), quoting.\n"
                        ),
                        code=2,
                    )
        try:
            # Operators need the pipeline/redirection executor even when the
            # first word is also a Python-backed built-in (for example
            # ``echo hello > file``).
            if any(x in line for x in ("|", ">", "<")):
                return self._exec_subprocess(line, timeout, env_extra=env_extra)
            if command in self.commands:
                return self._result(stdout=self.commands[command](parts[1:]))
            if command in {"git", "linux", "alpine"}:
                return self._exec_linux(command, parts[1:], timeout)
            if command in {"linux-setup", "gates", "zmux-pty-probe"}:
                return self._exec_zmux_command(command, parts[1:])
            if command in {"python", "python3"}:
                return self._exec_python_command(parts[1:])
            if command in {"pip", "zpip", "help", "zmux-info", "zmux-setup-storage"}:
                return self._exec_zmux_command(command, parts[1:])
            # A known external executable is run as a command. Everything else
            # is genuine Python source. (Lines containing |, >, < already went
            # to _exec_subprocess above — no need to test for them twice.)
            if self._is_external_command(command):
                return self._exec_subprocess(line, timeout, env_extra=env_extra)
            if command in self.KNOWN_TUI_COMMANDS:
                # Nothing installed by that name, and even if there were, a
                # full-screen TUI cannot render on ZMUX's pipe-based virtual
                # terminal. Say so instead of leaking a Python NameError.
                return self._result(
                    stderr=(
                        f"zmux: {command} needs a real TTY, which ZMUX does not "
                        "provide (no PTY).\n"
                        "  Non-interactive use can still go through the Alpine "
                        f"sandbox, e.g.:  linux {command} --help\n"
                        "  To edit files, use the Python runtime:  python\n"
                        "    (open('file','w').write(...))  or  cat > file\n"
                    ),
                    code=1,
                )
            # Legacy host-console fallback: preserve old Python escape-hatch
            # behavior until callers are migrated. Alpine PTY users should not
            # reach this path for normal shell commands. A strict opt-in flag
            # lets tests/callers preview command-only behavior without changing
            # the default compatibility contract.
            if self._strict_host_commands() and self._looks_like_command(line):
                return self._command_not_found(line)
            return self._exec_python(line, origin=line)
        except (OSError, ValueError) as exc:
            return self._result(stderr=f"{command}: {exc}\n", code=1)

    def _result(self, stdout: str = "", stderr: str = "", code: int = 0,
                streamed: tuple = ()) -> dict:
        """Build a terminal-style result.

        ``streamed`` names the streams already pushed to :attr:`output_sink`
        while the command ran (``"stdout"`` / ``"stderr"``). The interactive
        terminal uses it to emit only what has *not* been shown yet, so
        streamed output is never duplicated and non-streaming paths (built-in
        commands, zpip) still render. REST callers ignore it and read the
        complete text from ``stdout``/``stderr`` as before.
        """
        return {"ok": code == 0, "stdout": stdout, "stderr": stderr,
                "exit_code": code, "status": "idle", "streamed": streamed}

    def _path(self, value: str) -> Path:
        value = os.path.expanduser(value)
        p = Path(value)
        return (p if p.is_absolute() else self.cwd / p).resolve()

    #: Option letters accepted by _cmd_ls (-a -l -R -t -r, in any cluster).
    #: Anything else is rejected loudly instead of being swallowed:
    #: `ls -R` and `ls -t` previously exited 0 with plain output — the same
    #: silent-failure class as the operator guards — and `ls --color` was
    #: accepted and ignored. An honest terminal either implements a flag or
    #: says it does not.
    _LS_OPTIONS = frozenset("alRtr")

    def _cmd_ls(self, args: list[str]) -> str:
        show_all = long = recursive = by_time = reverse = False
        operands: list[str] = []
        after_ddash = False
        for arg in args:
            if after_ddash:
                operands.append(arg)
                continue
            if arg == "--":
                after_ddash = True
                continue
            if arg.startswith("-") and arg != "-":
                letters = arg[1:]
                if not letters or not set(letters) <= self._LS_OPTIONS:
                    shown = arg if arg.startswith("--") else letters[0]
                    raise ValueError(f"invalid option -- '{shown}'")
                show_all |= "a" in letters
                long |= "l" in letters
                recursive |= "R" in letters
                by_time |= "t" in letters
                reverse |= "r" in letters
                continue
            operands.append(arg)
        targets = [self._path(value) for value in (operands or ["."])]
        for target in targets:
            if not target.exists():
                raise FileNotFoundError(f"No such file or directory: '{target}'")
        if recursive:
            return self._ls_recursive(targets, show_all, long, by_time, reverse)
        return self._ls_flat(targets, show_all, long, by_time, reverse)

    @staticmethod
    def _ls_sort_key(path: Path, by_time: bool):
        return path.stat().st_mtime if by_time else path.name

    def _ls_entries(self, path: Path, show_all: bool, by_time: bool, reverse: bool) -> list:
        entries = [p for p in path.iterdir() if show_all or not p.name.startswith(".")]
        # `-t` lists newest first by default (so reverse=False still means
        # descending mtime); `-r` flips either ordering.
        descending = reverse if not by_time else not reverse
        entries.sort(key=lambda p: self._ls_sort_key(p, by_time), reverse=descending)
        return entries

    @staticmethod
    def _ls_format(entries: list, long: bool) -> str:
        if long:
            return "".join(f"{p.stat().st_mode:06o} {p.stat().st_size:>8} {p.name}\n" for p in entries)
        names = [p.name for p in entries]
        return "  ".join(names) + ("\n" if names else "")

    def _ls_flat(self, targets: list, show_all: bool, long: bool,
                 by_time: bool, reverse: bool) -> str:
        """List one or more targets; multiple targets get GNU-style headers."""
        sections = []
        for target in targets:
            if target.is_dir() and not target.is_symlink():
                entries = self._ls_entries(target, show_all, by_time, reverse)
                body = self._ls_format(entries, long)
            else:
                body = f"{target.name}\n"
            sections.append(f"{target}:\n{body}" if len(targets) > 1 else body)
        return "\n".join(sections) if len(targets) > 1 else "".join(sections)

    def _ls_recursive(self, targets: list, show_all: bool, long: bool,
                      by_time: bool, reverse: bool) -> str:
        """Recursive listing, depth-first, with `path:` headers (GNU -R shape).

        An explicit stack keeps the walk iterative so a deep tree can never
        trip the Python recursion limit, and symlinked directories are not
        followed (prevents cycles, matches GNU ls -R default).
        """
        sections = []
        for target in targets:
            if not (target.is_dir() and not target.is_symlink()):
                sections.append((None, f"{target.name}\n"))
                continue
            stack = [target]
            while stack:
                current = stack.pop()
                entries = self._ls_entries(current, show_all, by_time, reverse)
                sections.append((current, self._ls_format(entries, long)))
                for entry in reversed(entries):
                    if entry.is_dir() and not entry.is_symlink():
                        stack.append(entry)
        rendered = []
        for header, body in sections:
            rendered.append(f"{header}:\n{body}" if header is not None else body)
        return "\n".join(rendered)

    def _cmd_mkdir(self, args: list[str]) -> str:
        parents = "-p" in args
        values = [a for a in args if not a.startswith("-")]
        if not values: raise ValueError("missing operand")
        for value in values: self._path(value).mkdir(parents=parents, exist_ok=parents)
        return ""

    # Options accepted by _cmd_rm. Anything else is rejected loudly instead of
    # being fuzzy-matched: previously any "-" argument *containing* the letter
    # "r"/"f" enabled recursive/force (e.g. `rm -random-flag dir/` would have
    # recursively deleted a directory the user never asked to touch).
    _RM_LONG_FLAGS = {"--recursive", "--force"}
    _RM_CLUSTER_LETTERS = frozenset("rRf")

    def _cmd_rm(self, args: list[str]) -> str:
        recursive = force = False
        for arg in args:
            if not arg.startswith("-"):
                continue
            if arg in self._RM_LONG_FLAGS:
                if arg == "--recursive":
                    recursive = True
                else:
                    force = True
            elif arg[1:] and set(arg[1:]) <= self._RM_CLUSTER_LETTERS:
                recursive = recursive or "r" in arg or "R" in arg
                force = force or "f" in arg
            else:
                shown = arg if arg.startswith("--") else arg[1:]
                raise ValueError(f"invalid option -- '{shown}'")
        values = [a for a in args if not a.startswith("-")]
        if not values: raise ValueError("missing operand")
        for value in values:
            path = self._path(value)
            try:
                if path.is_dir() and not path.is_symlink():
                    if not recursive: raise IsADirectoryError(path)
                    shutil.rmtree(path)
                else: path.unlink()
            except FileNotFoundError:
                if not force: raise
        return ""

    def _cmd_cp(self, args: list[str]) -> str:
        if len(args) != 2: raise ValueError("usage: cp SOURCE DEST")
        src, dest = self._path(args[0]), self._path(args[1])
        if dest.is_dir(): dest /= src.name
        shutil.copy2(src, dest); return ""

    def _cmd_mv(self, args: list[str]) -> str:
        if len(args) != 2: raise ValueError("usage: mv SOURCE DEST")
        shutil.move(str(self._path(args[0])), str(self._path(args[1]))); return ""

    def _cmd_cat(self, args: list[str]) -> str:
        if not args: raise ValueError("missing operand")
        return "".join(self._path(arg).read_text(encoding="utf-8") for arg in args)

    def _cmd_touch(self, args: list[str]) -> str:
        if not args: raise ValueError("missing operand")
        for arg in args: self._path(arg).touch(exist_ok=True)
        return ""

    def _cmd_echo(self, args: list[str]) -> str: return " ".join(args) + "\n"
    def _cmd_pwd(self, args: list[str]) -> str: return str(self.cwd) + "\n"
    def _cmd_clear(self, args: list[str]) -> str: return "\033[H\033[2J\033[3J"
    def _cmd_env(self, args: list[str]) -> str: return "".join(f"{k}={v}\n" for k, v in sorted(os.environ.items()))
    def _cmd_which(self, args: list[str]) -> str:
        found = []
        for arg in args:
            resolved = self._find_executable(arg)  # single lookup per name
            if resolved:
                found.append(resolved)
        return "".join(path + "\n" for path in found)
    def _cmd_uname(self, args: list[str]) -> str: return platform.platform() + "\n"

    def _cmd_cd(self, args: list[str]) -> str:
        if len(args) > 1: raise ValueError("too many arguments")
        # Resolve BOTH sides before comparing. On Android, APP_DIR often
        # arrives as /data/user/0/... which is a symlink to /data/data/...;
        # comparing the raw HOME_DIR against its own .resolve() failed and
        # bare `cd` (go home) reported "outside home directory" while
        # `cd <subdir>` worked (because _path() resolves). Resolving the
        # target fixes both and keeps the sandbox check intact.
        home = HOME_DIR.resolve()
        target = self._path(args[0]).resolve() if args else home
        # Keep the virtual terminal inside app-private storage. This avoids
        # exposing arbitrary device paths while preserving a real persistent
        # directory change for subsequent Python and subprocess operations.
        try:
            target.relative_to(home)
        except ValueError:
            raise PermissionError(f"cannot access '{target}': outside home directory")
        if not target.is_dir():
            raise FileNotFoundError(f"No such file or directory: '{target}'")
        self.cwd = target
        return ""

    @contextlib.contextmanager
    def _chdir_context(self):
        """Run in-process Python with the process cwd set to ``self.cwd``.

        Without this, ``cd`` only moved a *variable*: subprocesses got the new
        directory via ``Popen(cwd=...)``, but ``open("x.txt")`` inside user
        Python code still resolved against the process working directory. A
        user could write a file in Python and then find that ``ls`` and
        ``cat`` could not see it — two filesystems in one session.

        Known limitation: ``os.chdir`` is process-wide, while ZMUX runs
        several sessions in one process. Two sessions executing Python file
        I/O at the *same instant* can race. Serialising them with a lock was
        rejected: a background ``while True`` loop would then freeze every
        other tab, which is worse. Subprocesses are unaffected either way
        (they receive an explicit ``cwd=``).
        """
        target = str(self.cwd)
        try:
            previous = os.getcwd()
        except OSError:      # cwd deleted underneath us
            previous = str(HOME_DIR)
        try:
            os.chdir(target)
        except OSError:
            pass             # unreadable dir: fall back to the old behaviour
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.chdir(previous)

    @contextlib.contextmanager
    def _stdin_context(self):
        """Point sys.stdin at the installed provider while user code runs.

        (contextlib.redirect_stdin only exists on Python 3.12+; the embedded
        runtime is 3.11, so the swap is done by hand.)
        """
        if self.stdin_provider is None:
            yield
            return
        old_stdin = sys.stdin
        sys.stdin = self.stdin_provider
        try:
            yield
        finally:
            sys.stdin = old_stdin

    def _format_traceback(self) -> str:
        """Rich-rendered traceback when available; stdlib format otherwise."""
        rich = _get_rich()
        if rich is not None:
            Console, Traceback = rich
            buffer = io.StringIO()
            console = Console(
                file=buffer,
                force_terminal=True,
                width=max(20, self.width),
                color_system="standard",
                highlight=False,
            )
            console.print(Traceback(show_locals=False))
            return buffer.getvalue()
        return traceback.format_exc()

    def _make_sinks(self):
        """Return (stdout, stderr) sinks honouring :attr:`output_sink`.

        With a sink installed both streams stream live to the terminal; the
        text is still accumulated so the result dict stays complete.
        """
        if self.output_sink is None:
            return io.StringIO(), io.StringIO()
        return StreamingWriter(self.output_sink), StreamingWriter(self.output_sink)

    def _streamed(self) -> tuple:
        """Streams already forwarded live (empty when no sink is installed)."""
        return ("stdout", "stderr") if self.output_sink is not None else ()

    @staticmethod
    def _drain(stream) -> str:
        """Flush a sink (if streaming) and return everything written to it."""
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()
        return stream.getvalue()

    #: Command name: a bare word, optionally with dots/dashes/slashes.
    _COMMAND_WORD = re.compile(r"^[\w.@/-]+$")
    #: Argument: anything a real command takes (flags, URLs, paths, globs)
    #: with no whitespace. Lone Python operators are rejected separately, so
    #: `undefined_var + 1` and `x = = 5` remain Python and report genuine
    #: errors instead of being mistaken for commands.
    _COMMAND_ARG = re.compile(r"^[\w.@/:~=+*?,%#\[\]{}-]+$")
    #: Tokens that only ever appear in Python expressions, never as a bare
    #: command argument.
    _PYTHON_OPERATORS = frozenset(
        "+ - * / // % ** = == != < > <= >= | & ^ ~ @ := and or not in is if else for".split()
    )

    @staticmethod
    def _strict_host_commands() -> bool:
        """Whether to preview strict command-only handling in host shell mode."""
        return os.environ.get(STRICT_HOST_COMMANDS_ENV, "").strip().lower() in {
            "1", "true", "yes", "on"
        }

    def execute_command(self, line: str, timeout: float | None = None,
                        env_extra: dict | None = None) -> dict:
        """Execute one line as a command, never as implicit Python.

        This backs explicit REST ``language="command"``. It keeps known
        builtins/app-control/external commands working but returns shell-style
        command-not-found for anything else, including Python expressions.
        """
        line = line.strip()
        if not line:
            return self._result()
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return self._result(stderr=f"parse error: {exc}\n", code=2)
        if not parts:
            return self._result()
        command = parts[0]
        rejection = self._unsupported_operator(parts, line)
        if rejection is not None:
            return rejection
        if any(x in line for x in ("|", ">", "<")):
            return self._exec_subprocess(line, timeout, env_extra=env_extra)
        try:
            if command in self.commands:
                return self._result(stdout=self.commands[command](parts[1:]))
            if command in {"git", "linux", "alpine"}:
                return self._exec_linux(command, parts[1:], timeout)
            if command in {"linux-setup", "gates", "zmux-pty-probe"}:
                return self._exec_zmux_command(command, parts[1:])
            if command in {"python", "python3"}:
                return self._exec_python_command(parts[1:])
            if command in {"pip", "zpip", "help", "zmux-info", "zmux-setup-storage"}:
                return self._exec_zmux_command(command, parts[1:])
            if self._is_external_command(command):
                return self._exec_subprocess(line, timeout, env_extra=env_extra)
            if command in self.KNOWN_TUI_COMMANDS:
                return self._result(
                    stderr=(
                        f"zmux: {command} needs a real TTY, which ZMUX does not "
                        "provide (no PTY).\n"
                    ),
                    code=1,
                )
            return self._command_not_found(line)
        except (OSError, ValueError) as exc:
            return self._result(stderr=f"{command}: {exc}\n", code=1)

    def _command_not_found(self, source: str) -> dict:
        """Return a shell-style command-not-found result for a legacy line."""
        name = source.strip().split()[0]
        return self._result(stderr=f"zmux: {name}: command not found\n", code=127)

    def _looks_like_command(self, source: str) -> bool:
        """True when a failed legacy host-console line was probably a command.

        `gti status` is not valid Python and not a known program; reporting
        `SyntaxError: invalid syntax` for it is unhelpful, because the user
        was typing a shell command. Real Python (assignments, calls, literals)
        must never be diverted, so this only matches bare-word lines. This is a
        compatibility classifier for the old host console, not Alpine PTY UX.
        """
        stripped = source.strip()
        if not stripped or "\n" in stripped:
            return False
        tokens = stripped.split()
        first = tokens[0]
        if not self._COMMAND_WORD.match(first):
            return False
        for token in tokens[1:]:
            # A `-x`/`--flag` argument is unambiguous: Python would read the
            # dash as an operator, but no expression starts an operand that
            # way, so it marks the line as a command invocation.
            if token.startswith("-") and len(token) > 1 and token[1] not in "0123456789":
                continue
            # A standalone Python operator means this is an expression.
            if token in self._PYTHON_OPERATORS or not self._COMMAND_ARG.match(token):
                return False
        # Anything Python could legitimately resolve is not a typo.
        if first in self.globals or first in dir(builtins):
            return False
        if first in self._command_names() or first in {"exit", "quit"}:
            return False
        import keyword
        return not keyword.iskeyword(first) and not keyword.issoftkeyword(first)

    def _exec_python(self, source: str, origin: str | None = None) -> dict:
        """Execute Python source.

        ``origin`` marks a line that arrived through *shell* mode, where a
        syntax error more likely means a mistyped command than broken Python.
        """
        out, err = self._make_sinks()
        try:
            # eval gives REPL-like expression output; exec handles statements.
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    self._stdin_context(), self._chdir_context():
                try:
                    code = compile(source, "<zmux>", "eval")
                except SyntaxError:
                    code = compile(source, "<zmux>", "exec")
                    exec(code, self.globals, self.globals)
                else:
                    value = eval(code, self.globals, self.globals)
                    if value is not None: print(repr(value))
            return self._result(self._drain(out), self._drain(err), streamed=self._streamed())
        except KeyboardInterrupt:
            # Ctrl+C (async-injected or raised by user/stdin): mirror CPython's
            # own REPL — a one-line notice and exit status 130, no traceback.
            # Written through the sink so it streams like any other output.
            err.write("KeyboardInterrupt\n")
            return self._result(self._drain(out), self._drain(err), 130, streamed=self._streamed())
        except SystemExit as exc:
            # Quiet exit like the real REPL exiting a subshell: no traceback
            # spam; propagate the numeric code when one was given.
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            return self._result(self._drain(out), self._drain(err), code, streamed=self._streamed())
        except (SyntaxError, NameError) as error:
            # In shell mode a bare-word line that is neither a program nor
            # valid Python is a mistyped command; say so the way a shell does
            # (exit 127) instead of a confusing SyntaxError/NameError.
            # A single unknown word (`foo`) is a NameError; a word with
            # arguments (`gti status`) is a SyntaxError. Both mean the same
            # thing to someone typing at a shell prompt.
            if origin is not None and self._looks_like_command(origin):
                name = origin.strip().split()[0]
                # For a NameError, confirm the undefined name really came from
                # this line's command word before claiming "not found" — but
                # allow the leading fragment too, since Python parses
                # `git-foo` as `git - foo` and reports only `git`.
                head = re.split(r"[.\-/@]", name)[0]
                if isinstance(error, SyntaxError) or name in str(error) or (
                    head and f"'{head}'" in str(error)
                ):
                    err.write(f"zmux: {name}: command not found\n")
                    return self._result(self._drain(out), self._drain(err), 127,
                                        streamed=self._streamed())
            err.write(self._format_traceback())
            return self._result(self._drain(out), self._drain(err), 1, streamed=self._streamed())
        except BaseException:
            err.write(self._format_traceback())
            return self._result(self._drain(out), self._drain(err), 1, streamed=self._streamed())

    def _exec_python_command(self, args: list[str]) -> dict:
        if not args or args[0] in {"--version", "-V"}:
            suffix = "Use Python expressions directly at this prompt.\n" if not args else ""
            return self._result(stdout=f"Python {sys.version}\n{suffix}")
        if args[0] == "-c":
            if len(args) < 2: return self._result(stderr="python: argument expected for -c\n", code=2)
            return self._exec_python(args[1])
        script = self._path(args[0])
        try:
            source = script.read_text(encoding="utf-8")
        except OSError as exc:
            return self._result(stderr=f"python: {exc}\n", code=1)
        old_name, old_file, old_argv = self.globals.get("__name__"), self.globals.get("__file__"), sys.argv
        self.globals.update(__name__="__main__", __file__=str(script)); sys.argv = [str(script), *args[1:]]
        try: return self._exec_python(source)
        finally:
            self.globals["__name__"], self.globals["__file__"], sys.argv = old_name, old_file, old_argv

    def _exec_zmux_command(self, command: str, args: list[str]) -> dict:
        from zmux import cli, linuxenv, zpip
        out, err = self._make_sinks()
        # A wheel download (zpip) or rootfs download (linux-setup) is the
        # longest blocking thing these commands do; without a progress sink
        # the terminal simply stops responding for its duration. The bars are
        # written straight through (they use \r to repaint in place, which a
        # line-buffered sink would otherwise hold back).
        previous_zpip = zpip.progress_sink
        previous_linux = linuxenv.progress_sink
        if self.output_sink is not None:
            sink = lambda text: self.output_sink(
                text.replace("\n", "\r\n").encode("utf-8", errors="replace")
            )
            zpip.progress_sink = sink
            linuxenv.progress_sink = sink
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([command, *args])
        finally:
            zpip.progress_sink = previous_zpip
            linuxenv.progress_sink = previous_linux
        return self._result(self._drain(out), self._drain(err), code,
                            streamed=self._streamed())

    def _exec_linux(self, command: str, args: list[str], timeout: float | None) -> dict:
        """Run a command inside the proot'd Alpine userland.

        ``git`` runs the real ``/usr/bin/git`` from the Alpine rootfs with
        unchanged syntax — clone/branch/checkout/push behave exactly as they
        do on any Linux box. ``linux``/``alpine`` run a shell command
        (``/bin/sh -c ...``) for anything else (``apk add ...``,
        ``python3``, ``ls -R`` inside the sandbox, ...).
        """
        from zmux import linuxenv
        if not linuxenv.is_installed():
            return self._result(
                stderr="zmux: Alpine environment is not installed.\n"
                       "  Run `linux-setup` once (downloads ~4 MiB, verifies SHA-512,\n"
                       "  extracts into app-private storage). Then `git` works normally.\n",
                code=1,
            )
        try:
            if command == "git":
                if not (linuxenv.rootfs_dir() / "usr" / "bin" / "git").is_file():
                    return self._result(
                        stderr=("zmux: git is not installed inside the Alpine "
                                "environment yet.\n"
                                "  Run: linux apk add git openssh-client\n"),
                        code=1,
                    )
                guest_argv = ["/usr/bin/git", *args]
            elif command in ("linux", "alpine"):
                if args and args[0] in ("--help", "-h", "help"):
                    return self._result(
                        stdout=("Alpine Linux inside ZMUX (proot).\n"
                                "  linux              open the real interactive Alpine shell\n"
                                "                     (PTY: vim/htop/less/tmux work; exit returns)\n"
                                "  linux <command...>  run one shell command, e.g. linux apk add git\n"
                                "  git <args...>       real git, e.g. git clone <url>\n"
                                "  linux-setup         install/repair the environment\n"
                                "  gates               run the on-device acceptance tests\n"),
                    )
                if not args:
                    return self._result(
                        stdout=("Alpine Linux inside ZMUX (proot).\n"
                                "  linux              open the real interactive Alpine shell\n"
                                "                     (PTY: vim/htop/less/tmux work; exit returns)\n"
                                "  linux <command...>  run a shell command, e.g. linux apk add git\n"
                                "  git <args...>       real git, e.g. git clone <url>\n"
                                "  linux-setup         install/repair the environment\n"
                                "  gates               run the on-device acceptance tests\n"),
                    )
                script = " ".join(shlex.quote(a) for a in args)
                guest_argv = ["/bin/sh", "-c", script]
            else:  # pragma: no cover - dispatch only calls the three above
                return self._result(stderr=f"zmux: unknown linux command {command}\n", code=2)
            cmdline = linuxenv.build_command_line(guest_argv, self.cwd)
        except RuntimeError as error:
            return self._result(stderr=f"zmux: {error}\n", code=1)
        return self._exec_subprocess(cmdline, timeout, env_extra=linuxenv.proot_env())

    def _find_executable(self, command: str) -> str | None:
        """Resolve ``command`` against the ZMUX PATH.

        Uses the same PATH the children receive (BIN_DIR first, then the
        Android system directories), so the generated ``zpip``/``help``/
        ``zmux-info`` wrappers in BIN_DIR are reachable here too. Previously
        this searched a hardcoded list that omitted BIN_DIR entirely, making
        those wrappers unresolvable from the pipeline executor.
        """
        if "/" in command:
            return command if os.path.isfile(command) and os.access(command, os.X_OK) else None
        for directory in build_path().split(os.pathsep):
            candidate = os.path.join(directory, command)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK): return candidate
        return None

    #: Words that must never be treated as external programs, even when a
    #: same-named binary exists on PATH. `import` is the notorious one: it is
    #: ImageMagick's screenshot tool *and* the most common Python statement
    #: there is, so `import math` used to run ImageMagick and fail with
    #: "unable to open X server". The others are equally common statement
    #: keywords that ship as binaries on some systems.
    PYTHON_KEYWORD_GUARD = frozenset({
        "import", "from", "print", "exec", "eval", "assert", "del", "pass",
        "raise", "return", "yield", "lambda", "with", "while", "for", "if",
        "else", "elif", "try", "except", "finally", "class", "def", "global",
        "nonlocal", "not", "and", "or", "is", "in", "break", "continue",
        "async", "await", "match", "case", "true", "false", "none",
    })

    #: Full-screen TUI programs that need a real TTY. ZMUX is a virtual
    #: terminal (no /dev/ptmx, no openpty), so even when a same-named binary
    #: exists these only work for non-interactive use — and when they are not
    #: installed at all, typing the name used to fall through to Python and
    #: produce a baffling `NameError`. Intercepting them turns that into an
    #: honest explanation (see execute()).
    KNOWN_TUI_COMMANDS = frozenset({
        "nano", "vim", "vi", "emacs", "htop", "top", "less", "more",
        "micro", "joe", "mcedit", "ranger", "screen", "tmux",
    })

    def _is_external_command(self, command: str) -> bool:
        """True when ``command`` should be run as an external program.

        Python statement keywords are excluded so shell mode's Python escape
        hatch keeps working regardless of what happens to be installed on the
        device's PATH.
        """
        if command in self.PYTHON_KEYWORD_GUARD:
            return False
        return self._find_executable(command) is not None

    def _exec_subprocess(self, line: str, timeout: float | None,
                         env_extra: dict | None = None) -> dict:
        with self._procs_lock:
            self._subprocess_depth += 1
        try:
            return self._exec_subprocess_inner(line, timeout, env_extra)
        finally:
            with self._procs_lock:
                self._subprocess_depth -= 1

    def _read_stdout_streaming(self, proc, timeout: float | None, stream: bool) -> str:
        """Read ``proc`` stdout to EOF, forwarding chunks to the terminal.

        Returns the complete text so the REST result stays identical. When no
        output sink is installed (REST calls) or the pipeline redirects to a
        file, this simply accumulates without emitting.

        ``readline()`` blocks, so the read runs on a helper thread. The caller
        waits for the *process* to exit (bounded by ``timeout``); once it has,
        the read end is closed so the pump stops even if a grandchild
        inherited the pipe and kept it open — otherwise a finished `git clone`
        whose remote helper lingers would hang the session forever.
        """
        sink = self.output_sink if stream else None
        chunks: list = []
        handle = proc.stdout

        def pump() -> None:
            try:
                # Line-oriented: the smallest unit a terminal can usefully
                # render, and it avoids splitting escape sequences mid-line.
                for chunk in iter(handle.readline, ""):
                    chunks.append(chunk)
                    if sink is not None:
                        sink(chunk.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8", "replace"))
            except (ValueError, OSError):
                pass  # handle closed underneath us (timeout kill / stray pipe)

        reader = threading.Thread(target=pump, daemon=True, name="ZMUX-Stdout-Reader")
        reader.start()
        # Wait for the process itself, not the pipe: a child that inherited
        # stdout keeps the pipe open after the main process exits, and
        # waiting on readline there would hang forever (reader.join(None)).
        deadline = time.monotonic() + timeout if timeout is not None else None
        while proc.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            time.sleep(0.05)
        # Process is done. Do NOT try to force-close the pipe from here: the
        # pump may be blocked in readline() on the same handle, and close()
        # from another thread deadlocks against it. The pump is a daemon
        # thread — give it a moment to drain the tail (normal case: EOF
        # arrives immediately once the process's write end closes), then
        # return whatever we have if a grandchild is still holding the pipe.
        reader.join(timeout=1.0)
        return "".join(chunks)

    def _exec_subprocess_inner(self, line: str, timeout: float | None,
                               env_extra: dict | None = None) -> dict:
        # Parse pipelines ourselves; shell=True would reintroduce /system/bin/sh.
        lexer = shlex.shlex(line, posix=True, punctuation_chars="|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
        stages, current = [], []
        for token in tokens:
            if token == "|": stages.append(current); current = []
            else: current.append(token)
        stages.append(current)
        if any(not stage for stage in stages): return self._result(stderr="invalid pipeline\n", code=2)
        # Redirection is parsed here rather than delegated to sh.  Restrict it
        # to input on the first stage and output on the final stage, matching
        # the common command-line forms: ``cat < in | grep x > out``.
        input_file = output_file = None
        append = False
        cleaned = []
        for index, stage in enumerate(stages):
            argv = []
            pos = 0
            while pos < len(stage):
                token = stage[pos]
                if token in {"<", ">", ">>"}:
                    if pos + 1 >= len(stage): return self._result(stderr=f"missing redirection target after {token}\n", code=2)
                    target = self._path(stage[pos + 1])
                    if token == "<" and index == 0: input_file = target
                    elif token in {">", ">>"} and index == len(stages) - 1:
                        output_file, append = target, token == ">>"
                    else: return self._result(stderr="redirection must be at pipeline edge\n", code=2)
                    pos += 2; continue
                argv.append(token); pos += 1
            if not argv: return self._result(stderr="invalid command\n", code=2)
            cleaned.append(argv)
        previous, processes, source_handle = None, [], None
        stderr_parts: list[str] = []
        stderr_threads: list[threading.Thread] = []
        # Streaming is decided up front (before the spawn loop) because the
        # stderr drain threads need to know whether to forward lines live.
        # Long-running tools like `git clone` write ALL progress to stderr —
        # without this, a slow clone shows nothing on screen until it exits.
        did_stream = self.output_sink is not None and output_file is None
        try:
            if input_file: source_handle = input_file.open("r", encoding="utf-8")
            for index, stage in enumerate(cleaned):
                executable = self._find_executable(stage[0])
                if not executable: return self._result(stderr=f"{stage[0]}: command not found\n", code=127)
                stdin = previous.stdout if previous else source_handle
                # start_new_session gives every pipeline its own process group,
                # so interrupt() can killpg() it without signalling our own
                # server process (shared group would nuke the app itself).
                env = build_env(self.cwd)
                if env_extra:
                    env = {**env, **env_extra}
                proc = subprocess.Popen([executable, *stage[1:]], cwd=self.cwd, stdin=stdin,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        env=env, start_new_session=True)
                if previous: previous.stdout.close()
                processes.append(proc); previous = proc
                # Register for Ctrl+C signalling (interrupt() kills the group).
                with self._procs_lock:
                    self._procs.append(proc)
                # Close the spawn race: Ctrl+C may have been pressed in the
                # gap between worker start and Popen returning.
                if self._interrupt.is_set() and proc.poll() is None:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        if hasattr(os, "killpg"):
                            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                        else:
                            proc.send_signal(signal.SIGINT)
                # Drain every stderr concurrently. Waiting for only the last
                # pipeline member can deadlock when an earlier member writes a
                # large error stream. When the terminal is live-streaming
                # stdout, stream stderr the same way so progress (git clone,
                # apk, curl) is visible instead of arriving in one block at
                # the end.
                def drain(stream=proc.stderr):
                    if did_stream:
                        for chunk in iter(stream.readline, ""):
                            stderr_parts.append(chunk)
                            self.output_sink(
                                chunk.replace("\r\n", "\n").replace("\n", "\r\n")
                                .encode("utf-8", "replace")
                            )
                    else:
                        stderr_parts.append(stream.read())
                reader = threading.Thread(target=drain, daemon=True)
                reader.start(); stderr_threads.append(reader)
            # Stream the final stage's stdout instead of communicate(): a
            # long-running child (ping, logcat, a build) must appear live in
            # the terminal, not arrive in one block when it exits. Redirected
            # output still goes to the file, never to the screen.
            stdout = self._read_stdout_streaming(
                processes[-1], timeout, stream=output_file is None
            )
            for proc in processes[:-1]: proc.wait(timeout=timeout)
            for reader in stderr_threads: reader.join(timeout=1)
            if output_file:
                with output_file.open("a" if append else "w", encoding="utf-8") as handle: handle.write(stdout)
                stdout = ""
            code = next((p.returncode for p in processes if p.returncode), 0)
            if code < 0:
                # Killed by a signal: say so plainly instead of printing a bare
                # negative exit code (Termux renders this as
                # "[Process completed (signal N)]").
                sig = -code
                hint = f"[process terminated by signal {sig}"
                if sig == 9:
                    hint += " — SIGKILL; on Android 12+ this is often the OS phantom-process limit"
                hint += "]\n"
                stderr_parts.append(hint)
                # The hint is added after the live stderr drain finished;
                # since stderr is marked streamed (not re-rendered by the
                # session layer), push it through the sink so the user still
                # sees it.
                if did_stream:
                    self.output_sink(
                        hint.replace("\r\n", "\n").replace("\n", "\r\n")
                        .encode("utf-8", "replace")
                    )
            # stdout and (when live-streaming) stderr were forwarded live;
            # the full text is still in the result for REST callers, and the
            # session layer skips whatever is marked streamed.
            return self._result(stdout, "".join(stderr_parts), code,
                                streamed=("stdout", "stderr") if did_stream else ())
        except subprocess.TimeoutExpired:
            for proc in processes: proc.kill()
            return self._result(stderr=f"Command timed out after {timeout}s\n", code=1)
        finally:
            with self._procs_lock:
                self._procs = [p for p in self._procs if p not in processes]
            if source_handle: source_handle.close()
