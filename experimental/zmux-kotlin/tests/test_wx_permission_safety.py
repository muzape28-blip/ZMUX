#!/usr/bin/env python3
"""W^X / "Permission denied" safety gates — the whole stack, nothing mocked.

Regression tests for the production failure seen on device after closing and
re-opening the app:

    : /data/user/0/com.zmux.terminal.debug/files/.zmuxrc[1]:
      /data/user/0/com.zmux.terminal.debug/files/bin/clear: Permission denied

Root cause: apps targeting Android 10+ (this APK targets SDK 34) cannot
``execve()`` ANY regular file inside their private data directory — SELinux
answers EACCES regardless of chmod 0755 (the kernel "W^X" rule). The Python
backend materializes CLI wrappers under ``files/bin`` and the Kotlin bootstrap
shell put that directory at the FRONT of PATH, so every lookup that landed on
a wrapper (including ``clear``, shadowing the real toybox applet) dead-ended
in "Permission denied".

These gates verify, end to end:

  A. Python runtime (fresh interpreter, Android-like environment):
     wrappers are still written/chmod 0755 for interpreter use, but BIN_DIR
     never reaches PATH and no wrapper name can resolve into the non-executable
     app sandbox.
  B. Desktop runtime semantics unchanged (wrappers stay first on PATH).
  C. The shipped Kotlin/Java source contracts: bootstrap PATH is system-only,
     ``linux-setup`` runs through the ``sh`` interpreter (read, not exec), the
     guest launcher mirrors every guard the Python launcher has, and an
     already-installed rootfs is auto-reopened on activity recreation.

Run: PYTHONDONTWRITEBYTECODE=1 python3 tests/test_wx_permission_safety.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PROJECT_ROOT / "app" / "src" / "main" / "python"
JAVA_HELPER = (
    PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "termux" / "terminal"
    / "TerminalSessionHelper.java"
)
KOTLIN_ACTIVITY = (
    PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "zmux" / "terminal"
    / "ZmuxTerminalActivity.kt"
)
KEY_CAP_VIEW = (
    PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "zmux" / "terminal"
    / "widget" / "KeyCapView.kt"
)

_WRAPPER_PROBE = r"""
import json, os, stat, sys
from pathlib import Path

import zmux.paths as paths
import zmux.env as zenv
from zmux.command_registry import WRAPPER_COMMANDS

report = {
    "app_dir": str(paths.APP_DIR),
    "bin_dir": str(paths.BIN_DIR),
    "blocked": paths.android_exec_blocked(),
    "path_env": os.environ.get("PATH", ""),
    "build_path": zenv.build_path(),
    "build_env_path": zenv.build_env()["PATH"],
    "wrappers": {},
}
for name in WRAPPER_COMMANDS:
    wrapper = Path(paths.BIN_DIR) / name
    mode = wrapper.stat().st_mode if wrapper.is_file() else None
    report["wrappers"][name] = None if mode is None else oct(stat.S_IMODE(mode))
print("PROBE_JSON:" + json.dumps(report))
"""


def _probe(payload: str, env_extra: dict, python_path: Path) -> dict:
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONPATH"] = str(python_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", payload],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"probe failed (exit={result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    marker = "PROBE_JSON:"
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise AssertionError(f"probe produced no report marker:\n{result.stdout}")


def _mksh_resolve(path_value: str, command: str) -> list[str]:
    """Emulate mksh PATH resolution: every candidate that exists on disk.

    mksh walks PATH in order and execve()'s the first hit. Under W^X the
    kernel answers EACCES for app-private files, so a single candidate inside
    the sandbox is already a guaranteed "Permission denied" — the simulation
    must therefore report *none* for Android-shaped PATHs.
    """
    hits = []
    for directory in path_value.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / command
        if candidate.is_file():
            hits.append(str(candidate))
    return hits


class AndroidWxRuntimeTests(unittest.TestCase):
    """A. Fresh interpreter behind ANDROID_PRIVATE = the on-device sandbox."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="zmux-wx-android-")
        cls.app_dir = Path(cls.temp.name)
        cls.report = _probe(
            _WRAPPER_PROBE,
            # An inherited PATH with a stale app-sandbox prepend must also be
            # scrubbed — that is exactly how an old process forward-pollutes.
            {
                "ANDROID_PRIVATE": str(cls.app_dir),
                "PATH": str(cls.app_dir / "bin") + os.pathsep + os.environ.get("PATH", ""),
            },
            PYTHON_ROOT,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_wx_block_detected(self) -> None:
        self.assertTrue(self.report["blocked"])

    def test_wrappers_materialised_and_executable_for_interpreter_use(self) -> None:
        missing = [n for n, m in self.report["wrappers"].items() if m is None]
        self.assertEqual(missing, [])
        bad_mode = {n: m for n, m in self.report["wrappers"].items() if m != "0o755"}
        self.assertEqual(bad_mode, {})

    def test_import_never_prepends_bin_dir_to_process_path(self) -> None:
        bin_dir = self.report["bin_dir"]
        self.assertNotIn(bin_dir, self.report["path_env"].split(os.pathsep))

    def test_build_path_contains_no_app_private_entry(self) -> None:
        app_prefix = self.report["app_dir"] + os.path.sep
        for field in ("build_path", "build_env_path"):
            entries = self.report[field].split(os.pathsep)
            leaked = [e for e in entries if e.startswith(app_prefix) or e == self.report["bin_dir"]]
            self.assertEqual(leaked, [], f"{field} leaks sandbox entries: {leaked}")
            self.assertIn("/system/bin", entries)

    def test_no_wrapper_command_can_resolve_into_the_sandbox(self) -> None:
        app_prefix = self.report["app_dir"] + os.path.sep
        path_value = self.report["build_env_path"]
        offenders = {}
        for name in self.report["wrappers"]:
            hits = _mksh_resolve(path_value, name)
            sandboxed = [h for h in hits if h.startswith(app_prefix)]
            if sandboxed:
                offenders[name] = sandboxed
        self.assertEqual(offenders, {})

    def test_clear_never_shadowed_by_wrapper(self) -> None:
        # The exact on-device failure: `.zmuxrc[1]` executing clear.
        app_prefix = self.report["app_dir"] + os.path.sep
        hits = _mksh_resolve(self.report["build_env_path"], "clear")
        self.assertFalse([h for h in hits if h.startswith(app_prefix)])


class DesktopRuntimeSemanticsTests(unittest.TestCase):
    """B. Off-device behavior must stay unchanged (wrappers usable on PATH)."""

    def test_bin_dir_still_prepended_on_desktop(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="zmux-wx-desktop-")
        try:
            package = Path(temp.name) / "host"
            shutil.copytree(PYTHON_ROOT, package)
            env_extra = {"PATH": os.environ.get("PATH", "")}
            for key in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH", "ZMUX_NATIVE_LIBRARY_DIR"):
                env_extra[key] = ""
            # Empty strings above would still trip os.environ checks in the
            # subprocess only if exported; filter them out entirely.
            env = dict(os.environ)
            env.pop("ANDROID_PRIVATE", None)
            env.pop("ANDROID_ARGUMENT", None)
            env.pop("ANDROID_APP_PATH", None)
            env.pop("ZMUX_NATIVE_LIBRARY_DIR", None)
            env["PYTHONPATH"] = str(package)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", _WRAPPER_PROBE],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                cwd=temp.name,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = "PROBE_JSON:"
            report = None
            for line in result.stdout.splitlines():
                if line.startswith(marker):
                    report = json.loads(line[len(marker):])
            self.assertIsNotNone(report, result.stdout)
            self.assertFalse(report["blocked"])
            self.assertEqual(report["build_path"].split(os.pathsep)[0], report["bin_dir"])
        finally:
            temp.cleanup()


class KotlinShellContractTests(unittest.TestCase):
    """C. Static contracts of the actual shipped shell-launch code."""

    @staticmethod
    def _method_block(src: str, name: str) -> str:
        # A Java method ends at its closing brace on a 4-space line; inner
        # braces are indented deeper, and comment text can legally contain the
        # words public/private/static, so a keyword lookahead is unreliable.
        match = re.search(name + r"\b.*?^    \}", src, re.S | re.M)
        return match.group(0) if match else ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.helper_src = JAVA_HELPER.read_text(encoding="utf-8")
        cls.activity_src = KOTLIN_ACTIVITY.read_text(encoding="utf-8")
        cls.keycap_src = KEY_CAP_VIEW.read_text(encoding="utf-8")
        cls.local_block = cls._method_block(cls.helper_src, "createLocalSession")
        cls.linux_block = cls._method_block(cls.helper_src, "createLinuxSession")

    def test_bootstrap_path_is_system_only(self) -> None:
        self.assertIn('PATH=/system/bin:/system/xbin:/vendor/bin', self.local_block)
        # The old bug: binDir embedded into the bootstrap PATH at all.
        self.assertNotIn("binDir.getAbsolutePath() + \":/", self.local_block)
        self.assertNotIn("\"PATH=\" + binDir", self.local_block)

    def test_bootstrap_path_cannot_hit_app_sandbox_for_clear(self) -> None:
        # Simulate mksh resolution of the exact PATH shipped to the shell with
        # the app-private bin populated — no hit may come from the sandbox.
        temp = tempfile.TemporaryDirectory(prefix="zmux-wx-mksh-")
        try:
            bin_dir = Path(temp.name) / "files" / "bin"
            bin_dir.mkdir(parents=True)
            trap = bin_dir / "clear"
            trap.write_text("#!/system/bin/sh\n", encoding="utf-8")
            trap.chmod(0o755)
            shipped_path = "/system/bin:/system/xbin:/vendor/bin"
            hits = [h for h in _mksh_resolve(shipped_path, "clear") if h.startswith(temp.name)]
            self.assertEqual(hits, [])
        finally:
            temp.cleanup()

    def test_linux_setup_run_through_interpreter_not_exec(self) -> None:
        # Reading a script via `sh <path>` needs no exec permission (W^X-safe).
        self.assertRegex(self.local_block, r"alias linux-setup='sh ")
        self.assertNotRegex(self.local_block, r"alias linux-setup='(?!sh\b)")

    def test_guest_bin_sh_detection_resolves_guest_absolute_links(self) -> None:
        # Alpine /bin/sh -> /bin/busybox must not resolve against the host /.
        self.assertIn("guestRegularFile", self.helper_src)
        self.assertIn("readSymbolicLink", self.helper_src)

    def test_guest_launcher_mirrors_python_guards(self) -> None:
        self.assertIn("ensureTallocCompat", self.linux_block)
        # DNS must be written into the guest rootfs; the host
        # /etc/resolv.conf must NEVER be bind-mounted anymore (it is often a
        # 127.0.0.1 stub the PRoot guest cannot reach — the root cause of
        # "Temporary failure resolving" and the apk add hang).
        self.assertIn("ensureGuestResolvConf(rootfs)", self.linux_block)
        self.assertNotIn('"/etc/resolv.conf:/etc/resolv.conf"', self.linux_block)
        self.assertIn("canRead()", self.linux_block)
        self.assertIn("canExecute()", self.linux_block)
        # And the helper itself must carry public DNS fallbacks.
        resolv_block = self._method_block(self.helper_src, "ensureGuestResolvConf")
        self.assertIn("8.8.8.8", resolv_block)
        self.assertIn("1.1.1.1", resolv_block)

    def test_guest_prompt_is_branded_in_profile_d(self) -> None:
        # The "localhost:~#" prompt came from Alpine's /etc/profile default
        # overwriting our env PS1. We now drop a script in /etc/profile.d
        # which busybox ash sources *after* that default.
        self.assertIn("ensureGuestPrompt(rootfs)", self.linux_block)
        prompt_block = self._method_block(self.helper_src, "ensureGuestPrompt")
        self.assertIn("etc/profile.d", prompt_block)
        self.assertIn("zmux-prompt.sh", prompt_block)
        self.assertIn("202mZMUX", prompt_block)
        self.assertNotIn("ZMUX@", prompt_block)
        self.assertNotIn("localhost:~", prompt_block)

    def test_setup_confirms_before_install_and_prompt_is_simple(self) -> None:
        self.assertIn("Install Alpine now? [y/N]", self.local_block)
        # Only one guest OS remains: no multi-option menu, no second OSC
        # install title, no second package manager, and no @os prompt.
        self.assertNotIn("Choose [", self.helper_src)
        self.assertNotIn("INSTALL_", self.helper_src.replace("INSTALL_ALPINE", ""))
        self.assertNotIn("ZMUX@", self.helper_src)
        self.assertNotIn("apt-get", self.helper_src)

    def test_pty_default_size_is_not_2000(self) -> None:
        # The constructor's 5th argument is the initial column count. It was
        # once 2000, which made `apk add python3` wrap the "3" onto a new
        # line before the PTY was ever resized to the real phone width.
        self.assertNotIn(", 2000,", self.helper_src)
        self.assertIn(", 80,", self.helper_src)

    def test_virtual_key_does_not_fire_on_touch_down(self) -> None:
        # Regression: keys used to call onFire() in ACTION_DOWN, so a finger
        # starting a horizontal scroll on the key bar immediately sent an
        # arrow/character before the scroll started. A proper tap must fire
        # only on ACTION_UP after the move stayed within touch slop.
        self.assertIn("scaledTouchSlop", self.keycap_src)
        self.assertIn("MotionEvent.ACTION_MOVE", self.keycap_src)
        self.assertIn("dragCanceled", self.keycap_src)
        self.assertIn("requestDisallowInterceptTouchEvent", self.keycap_src)
        down_block = re.search(
            r"MotionEvent\.ACTION_DOWN -> \{.*?\n\s*\}",
            self.keycap_src,
            re.S,
        )
        self.assertIsNotNone(down_block, "ACTION_DOWN block not found")
        self.assertNotIn("onFire?.invoke()", down_block.group(0))
        self.assertNotIn("onFire!!.invoke()", down_block.group(0))
        up_block = re.search(
            r"MotionEvent\.ACTION_UP -> \{.*?\n\s*\}",
            self.keycap_src,
            re.S,
        )
        self.assertIsNotNone(up_block, "ACTION_UP block not found")
        self.assertIn("onFire?.invoke()", up_block.group(0))

    def test_layout_changes_propagate_pty_size(self) -> None:
        # The onLayoutChangeListener must call TerminalView.updateSize() so
        # session.updateSize()/TIOCSWINSZ fires every time the view geometry
        # changes (first layout, rotation, IME resize). The dead onResize
        # callback must be gone — it was never assigned to anything.
        on_create = re.search(
            r"override fun onCreate\(.*?^    \}", self.activity_src, re.S | re.M
        )
        self.assertIsNotNone(on_create)
        block = on_create.group(0)
        self.assertIn("terminalView.updateSize()", block)
        self.assertNotIn("onResize", block)
        self.assertNotIn("onResize", Path(
            PROJECT_ROOT / "app/src/main/java/com/termux/terminal"
            / "ZmuxTerminalSession.kt"
        ).read_text(encoding="utf-8"))

    def test_theme_is_applied_immediately_on_attach(self) -> None:
        # The palette must be applied right after attachSession and MUST
        # notify the session via onColorsChanged() + invalidate(), otherwise
        # the renderer keeps Termux's default palette until the next output
        # (the "I changed colours but nothing happened" bug).
        self.assertIn("private fun applyThemeToView(", self.activity_src)
        self.assertIn("ZmuxTheme.applyToView(", self.activity_src)
        self.assertIn("applyThemeToView(s.session)", self.activity_src)
        self.assertIn("onColorsChanged", (
            PROJECT_ROOT / "app/src/main/java/com/zmux/terminal/ZmuxTheme.kt"
        ).read_text(encoding="utf-8"))
        self.assertIn("Typeface.MONOSPACE", (
            PROJECT_ROOT / "app/src/main/java/com/zmux/terminal/ZmuxTheme.kt"
        ).read_text(encoding="utf-8"))

    def test_terminal_session_client_interface_is_fully_implemented(self) -> None:
        # Termux TerminalSessionClient is an interface; missing any abstract
        # method fails the Kotlin compile (the Build Debug APK step). List
        # matches the 0.118.0 interface exactly — newer Termux releases add
        # e.g. setTerminalShellPid, which is why we don't claim it here.
        for method in (
            "onTextChanged", "onTitleChanged", "onSessionFinished",
            "onCopyTextToClipboard", "onPasteTextFromClipboard",
            "onBell", "onColorsChanged", "onTerminalCursorStateChange",
            "getTerminalCursorStyle",
        ):
            self.assertIn(method, self.activity_src, f"missing override: {method}")

    def test_installed_rootfs_auto_reopened_on_activity_recreate(self) -> None:
        self.assertIn("detectInstalledLinux()", self.activity_src)
        self.assertIn("etc/.zmux-rootfs", self.activity_src)
        self.assertIn('guestRegularFile(rootfs, "bin/sh")', self.activity_src)
        self.assertIn("libproot.so", self.activity_src)
        block = re.search(r"private fun createNewSession\(\).*?(?=\n    (private|override|fun))", self.activity_src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("detectInstalledLinux()", block.group(0))


class LocalShellRcEmulationTests(unittest.TestCase):
    """D. Execute the REAL .zmuxrc bytes the Java code emits, with traps set.

    The bootstrap rc is extracted from TerminalSessionHelper.java by evaluating
    the ``fw.write`` concatenations, then run under bash with an app-private
    bin directory full of executable traps. Nothing inside the app sandbox may
    ever be executed, and a negative control proves the old layout (app bin on
    PATH) would have fired the trap — the exact "Permission denied" class.
    """

    FAKE_FILES_DIR = "/data/user/0/com.zmux.terminal.debug/files"

    @classmethod
    def _extract_rc(cls) -> str:
        src = JAVA_HELPER.read_text(encoding="utf-8")
        start = src.index("FileWriter fw = new java.io.FileWriter(rc)")
        end = src.index("fw.close();", start)
        body = src[start:end]
        out = []
        token = re.compile(r'"(?:[^"\\]|\\.)*"|\besc\b|shellQuote\((.*?)\)\s*\+\s*|\)')
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("fw.write("):
                continue
            expr = line[len("fw.write("):].rstrip(";")
            text = ""
            idx = 0
            for match in token.finditer(expr):
                idx = match.end()
                value = match.group(0)
                if value == "esc":
                    text += "\x1b"
                elif value.startswith("shellQuote"):
                    inner = match.group(1)
                    path = cls._eval_java_string_concat(inner)
                    text += "'" + path.replace("'", "'\\''") + "'"
                elif value.startswith('"') and value != '"':
                    text += cls._java_unescape(value[1:-1])
            out.append(text)
        return "".join(out)

    @classmethod
    def _eval_java_string_concat(cls, expr: str) -> str:
        parts = []
        for match in re.finditer(r'"(?:[^"\\]|\\.)*"|binDir\.getAbsolutePath\(\)', expr):
            value = match.group(0)
            if value.startswith('"'):
                parts.append(cls._java_unescape(value[1:-1]))
            else:
                parts.append(cls.FAKE_FILES_DIR + "/bin")
        return "".join(parts)

    @staticmethod
    def _java_unescape(text: str) -> str:
        out = []
        idx = 0
        escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\x00", "7": "\x07"}
        while idx < len(text):
            char = text[idx]
            if char != "\\":
                out.append(char)
                idx += 1
                continue
            nxt = text[idx + 1]
            idx += 2
            if nxt in "01234567":  # octal escape (e.g. \033, \007)
                digits = nxt
                while idx < len(text) and len(digits) < 3 and text[idx] in "01234567":
                    digits += text[idx]
                    idx += 1
                out.append(chr(int(digits, 8) & 0xFF))
            else:
                out.append({"\\": "\\", '"': '"', "'": "'"}.get(nxt, escapes.get(nxt, nxt)))
        return "".join(out)

    def _run_rc(self, rc: str, path_env: str, app_dir: Path) -> subprocess.CompletedProcess:
        rc_file = app_dir / ".zmuxrc"
        rc_file.write_text(rc, encoding="utf-8")
        script = (
            "shopt -s expand_aliases; "
            'source "$1"; '
            'echo "ALIAS=$(alias linux-setup 2>/dev/null)"; '
            'echo "WHERE_CLEAR=$(type -a clear 2>/dev/null | tr "\\n" ";")"'
        )
        env = dict(os.environ)
        env["HOME"] = str(app_dir)
        env["PATH"] = path_env
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script, "_", str(rc_file)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _trapped_app_bin(self, app_dir: Path) -> Path:
        bin_dir = app_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ("clear", "help", "zpip", "pip", "linux", "gates", "linux-setup", "zmux-info"):
            trap = bin_dir / name
            trap.write_text(f"#!/bin/sh\necho TRAP-FIRED-{name}\n", encoding="utf-8")
            trap.chmod(0o755)
        return bin_dir

    def test_rc_executes_without_touching_app_sandbox(self) -> None:
        rc = self._extract_rc()
        self.assertIn("linux-setup", rc, "rc extraction failed — Java emitter drifted")
        temp = tempfile.TemporaryDirectory(prefix="zmux-rc-emu-")
        try:
            app_dir = Path(temp.name)
            # The device PATH is system-only. /system/* does not exist on CI,
            # so append the CI equivalents — mirroring what toybox provides on
            # device (clear, sh, printf...). The app bin stays OFF PATH.
            shipped_path = os.pathsep.join(
                ["/system/bin", "/system/xbin", "/vendor/bin", "/usr/bin", "/bin"]
            )
            result = self._run_rc(rc, shipped_path, app_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("TRAP-FIRED", result.stdout + result.stderr)
            self.assertNotIn("Permission denied", result.stdout + result.stderr)
            self.assertIn("WELCOME TO ZMUX", result.stdout)
            # `clear` must resolve to a system path — never the app sandbox.
            where = re.search(r"WHERE_CLEAR=(.*)", result.stdout)
            self.assertIsNotNone(where)
            self.assertNotIn(str(app_dir), where.group(1))
            # linux-setup resolves through the `sh` interpreter (read, not exec).
            alias = re.search(r"ALIAS=alias linux-setup='(.*)'", result.stdout)
            self.assertIsNotNone(alias, result.stdout)
            self.assertTrue(alias.group(1).startswith("sh "), alias.group(1))
        finally:
            temp.cleanup()

    def test_negative_control_old_layout_would_fire_the_trap(self) -> None:
        # Guard against a vacuous test: with the pre-fix PATH (app bin FIRST),
        # the very same rc executes the sandbox trap — which on device is the
        # kernel-denied execve() reported as "Permission denied".
        rc = self._extract_rc()
        temp = tempfile.TemporaryDirectory(prefix="zmux-rc-emu-old-")
        try:
            app_dir = Path(temp.name)
            bin_dir = self._trapped_app_bin(app_dir)
            old_path = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
            result = self._run_rc(rc, old_path, app_dir)
            self.assertIn("TRAP-FIRED-clear", result.stdout)
        finally:
            temp.cleanup()


def load_tests(loader, tests, pattern):  # noqa: ARG001 - unittest protocol
    """Also run the APP_DIR alignment gates from this entry point.

    CI (.github/workflows/ci.yml) invokes the test files by name, and that
    workflow is not always editable from the branch that adds a suite. The
    APP_DIR alignment gates in ``tests/test_app_dir_alignment.py`` guard the
    same production failure class as this file — a path the Kotlin host and the
    Python runtime disagree about — so they must never be skipped just because
    a workflow step was not added. ``make test`` also runs them on their own.
    """
    sibling = loader.discover(
        start_dir=str(Path(__file__).resolve().parent),
        pattern="test_app_dir_alignment.py",
    )
    if sibling.countTestCases() == 0:
        # unittest discovery is silent about a missing/renamed file, which would
        # turn this hook into a green run that verifies nothing.
        raise RuntimeError(
            "tests/test_app_dir_alignment.py loaded zero gates; refusing to report "
            "a passing run without the APP_DIR alignment checks."
        )
    tests.addTests(sibling)
    return tests


if __name__ == "__main__":
    unittest.main(verbosity=2)
