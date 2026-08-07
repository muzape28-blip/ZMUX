#!/usr/bin/env python3
"""Tests for the adaptive PROOT_NO_SECCOMP decision (seccomp-accelerator probe).

Why this exists: the old launcher set ``PROOT_NO_SECCOMP=1`` unconditionally to
avoid SIGSYS ("Bad system call") deaths on kernels whose seccomp policy rejects
proot's accelerator. That blanket setting forces *every* syscall through ptrace
and cost ~10-20x on syscall storms — measured `apk add fastfetch` = 20s in ZMUX
vs <1s in proot-distro on the very same device. The launcher now probe-caches
the verdict per proot build (accelerated when a trivial traced child exits 0,
slow-safe on any failure) — these tests pin the semantics and the Java parity.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PROJECT_ROOT / "app" / "src" / "main" / "python"

_TEST_APP_DIR = tempfile.TemporaryDirectory(prefix="zmux-seccomp-test-app-")
os.environ.setdefault("ANDROID_PRIVATE", _TEST_APP_DIR.name)
sys.path.insert(0, str(PYTHON_ROOT))
linuxenv = importlib.import_module("zmux.linuxenv")

JAVA_HELPER = (
    PROJECT_ROOT
    / "app" / "src" / "main" / "java"
    / "com" / "termux" / "terminal"
    / "TerminalSessionHelper.java"
)


def _fake_proot(directory: Path, name: str, exit_code: int) -> str:
    script = directory / name
    script.write_text("#!/bin/sh\nexit %d\n" % exit_code)
    script.chmod(0o755)
    return str(script)


class ProbeBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="zmux-seccomp-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.files_dir = self.tmp / "files"
        self.files_dir.mkdir()

    def test_accelerated_verdict_is_cached(self) -> None:
        ok = _fake_proot(self.tmp, "proot_ok", 0)
        assert linuxenv._proot_needs_no_seccomp(ok, self.files_dir) is False
        marker = (self.files_dir / linuxenv.SECCOMP_MODE_MARKER).read_text().splitlines()
        self.assertEqual(marker[0].strip(), "accelerated")
        self.assertTrue(marker[1].strip().startswith(ok + ":"))

    def test_sigsys_exit_means_no_seccomp(self) -> None:
        # 159 = 128 + 31: what a process reports when killed by SIGSYS.
        bad = _fake_proot(self.tmp, "proot_bad", 159)
        assert linuxenv._proot_needs_no_seccomp(bad, self.files_dir) is True
        marker = (self.files_dir / linuxenv.SECCOMP_MODE_MARKER).read_text().splitlines()
        self.assertEqual(marker[0].strip(), "no_seccomp")

    def test_timeout_means_no_seccomp(self) -> None:
        sleepy = self.tmp / "proot_sleepy"
        sleepy.write_text("#!/bin/sh\nsleep 30\n")
        sleepy.chmod(0o755)
        with patch.object(linuxenv.subprocess, "run", side_effect=linuxenv.subprocess.TimeoutExpired("x", 5)):
            assert linuxenv._proot_needs_no_seccomp(str(sleepy), self.files_dir) is True

    def test_missing_proot_falls_back_safe(self) -> None:
        assert linuxenv._proot_needs_no_seccomp(None, self.files_dir) is True
        ghost = str(self.tmp / "no-such-proot")
        assert linuxenv._proot_needs_no_seccomp(ghost, self.files_dir) is True

    def test_binary_change_invalidates_cache(self) -> None:
        bad = _fake_proot(self.tmp, "proot_flip", 159)
        assert linuxenv._proot_needs_no_seccomp(bad, self.files_dir) is True
        # Same path, new content + mtime: the verdict must be re-probed.
        p = Path(bad)
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        os.utime(p, (2_000_000_000, 2_000_000_000))
        assert linuxenv._proot_needs_no_seccomp(bad, self.files_dir) is False

    def test_nonzero_probe_exit_is_no_seccomp(self) -> None:
        for code in (1, 126, 127, 159):
            d = self.tmp / f"case-{code}"
            d.mkdir()
            fd = d / "files"
            fd.mkdir()
            bad = _fake_proot(d, "proot_bad", code)
            assert linuxenv._proot_needs_no_seccomp(bad, fd) is True, code


class ProotEnvIntegrationTests(unittest.TestCase):
    def test_env_adds_flag_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmux-env-bad-") as raw, patch.object(
            linuxenv, "_proot_needs_no_seccomp", return_value=True
        ):
            env = linuxenv.proot_env()
        self.assertEqual(env.get("PROOT_NO_SECCOMP"), "1")

    def test_env_omits_flag_when_accelerated(self) -> None:
        with patch.object(linuxenv, "_proot_needs_no_seccomp", return_value=False):
            env = linuxenv.proot_env()
        self.assertNotIn("PROOT_NO_SECCOMP", env)

    def test_env_defaults_safe_without_proot(self) -> None:
        # No Android, no host proot: the historical slow-but-universal mode.
        with patch.object(linuxenv, "proot_binary", return_value=None):
            env = linuxenv.proot_env()
        self.assertEqual(env.get("PROOT_NO_SECCOMP"), "1")


class JavaParityContractTests(unittest.TestCase):
    """The Kotlin launcher builds the same session: the verdict logic must not drift."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.java_src = JAVA_HELPER.read_text()

    def test_shared_marker_filename(self) -> None:
        self.assertIn(
            '"%s"' % linuxenv.SECCOMP_MODE_MARKER,
            self.java_src,
            "Java and Python launchers must share the verdict file name",
        )

    def test_probe_runs_trivial_traced_child(self) -> None:
        self.assertIn('"/system/bin/true"', self.java_src)
        for flag in ('"-0"', '"--kill-on-exit"', '"-w"'):
            self.assertIn(flag, self.java_src)

    def test_probe_strips_inherited_flag(self) -> None:
        # Both sides must probe WITHOUT the flag, or the probe would test the
        # slow path while meaning to test the fast one.
        self.assertIn('penv.remove("PROOT_NO_SECCOMP")', self.java_src)
        py_src = (PYTHON_ROOT / "zmux" / "linuxenv.py").read_text()
        self.assertIn('env.pop("PROOT_NO_SECCOMP", None)', py_src)

    def test_stamp_format_is_binary_identity(self) -> None:
        # path : mtime-seconds : size, on both launchers.
        self.assertRegex(self.java_src, r"lastModified\(\) / 1000L")
        py_src = (PYTHON_ROOT / "zmux" / "linuxenv.py").read_text()
        self.assertIn("int(st.st_mtime)", py_src)
        self.assertIn("st.st_size", py_src)

    def test_env_flag_is_conditional_in_java(self) -> None:
        pattern = re.compile(
            r"if \(prootNeedsNoSeccomp\(filesDir, prootPath, ldLibraryPath, loader, cache\)\)\s*"
            r"\{\s*envList\.add\(\"PROOT_NO_SECCOMP=1\"\);",
            re.S,
        )
        self.assertRegex(self.java_src, pattern)

    def test_verdict_words_match(self) -> None:
        py_src = (PYTHON_ROOT / "zmux" / "linuxenv.py").read_text()
        for word in ('"no_seccomp"', '"accelerated"'):
            self.assertIn(word, self.java_src)
            self.assertIn(word, py_src)


if __name__ == "__main__":
    unittest.main()
