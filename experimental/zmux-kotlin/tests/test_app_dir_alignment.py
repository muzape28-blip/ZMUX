#!/usr/bin/env python3
"""APP_DIR alignment gates — Kotlin host and Python runtime must agree.

Regression tests for the production failure seen on device right after a
*successful* install ("checksum verified", "rootfs installed and verified
successfully!"):

    [Chaquopy Error] Rootfs path disappeared before PRoot launch:
    /data/user/0/com.zmux.terminal.debug/files/linux/rootfs

Root cause (reproduced verbatim by :class:`ChaquopyLayoutTests`): Chaquopy is
not python-for-android. It exports none of the ANDROID_* variables and extracts
the app's Python sources to ``<filesDir>/chaquopy/AssetFinder/app``, so
``zmux.paths.resolve_app_dir()`` fell through to its ``__file__``-relative step,
found that (writable) extraction directory and adopted it as APP_DIR. Every
APP_DIR child then moved with it — ``linux/rootfs``, ``bin`` wrappers,
``.zmux_auth_token`` (read by ZmuxBackendLocator), ``cache``, ``logs`` — while
the Kotlin activity kept looking under ``filesDir``.

The contract these gates lock in:

  A. Python: ANDROID_PRIVATE (exported by the activity before ``Python.start()``)
     wins; Chaquopy's asset tree is never a runtime root; desktop/CI behaviour
     is unchanged.
  B. Layout: every path the Kotlin host consumes resolves under filesDir.
  C. Migration: a rootfs stranded by a pre-alignment build is adopted by rename,
     the user home is merged without overwriting, and nothing is destroyed.
  D. Kotlin source contract: no hand-built "linux/rootfs", paths come from
     ``zmux.linuxenv``, and the ANDROID_PRIVATE export precedes both
     ``Python.start()`` and the first ``zmux`` import.

Run: PYTHONDONTWRITEBYTECODE=1 python3 tests/test_app_dir_alignment.py
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PROJECT_ROOT / "app" / "src" / "main" / "python"
KOTLIN_ACTIVITY = (
    PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "zmux" / "terminal"
    / "ZmuxTerminalActivity.kt"
)
JAVA_HELPER = (
    PROJECT_ROOT / "app" / "src" / "main" / "java" / "com" / "termux" / "terminal"
    / "TerminalSessionHelper.java"
)

#: Printed by the probe below; every value the Kotlin host depends on.
_LAYOUT_PROBE = r"""
import json, os
import zmux.paths as paths
import zmux.linuxenv as linuxenv
import zmux.security as security

print("PROBE_JSON:" + json.dumps({
    "app_dir": str(paths.APP_DIR),
    "bin_dir": str(paths.BIN_DIR),
    "home_dir": str(paths.HOME_DIR),
    "cache_dir": str(paths.CACHE_DIR),
    "log_dir": str(paths.LOG_DIR),
    "token_file": str(security.TOKEN_FILE),
    "rootfs_dir": str(linuxenv.rootfs_dir()),
    "linuxenv_home_dir": str(linuxenv.home_dir()),
    "blocked": paths.android_exec_blocked(),
    "legacy_candidates": [str(p) for p in paths.legacy_app_dir_candidates()],
}))
"""


def _chaquopy_tree(root: Path) -> Path:
    """Build the real on-device layout: files/chaquopy/AssetFinder/app/zmux/…"""
    app_assets = root / "chaquopy" / "AssetFinder" / "app"
    app_assets.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PYTHON_ROOT, app_assets)
    return app_assets


def _probe(python_path: Path, env_extra: dict, cwd: Path) -> dict:
    """Run the layout probe in a fresh interpreter (APP_DIR is import-time state)."""
    env = dict(os.environ)
    for key in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH",
                "ZMUX_NATIVE_LIBRARY_DIR", "ZMUX_ROOTFS_DIR", "ZMUX_RUNTIME_LIB_DIR"):
        env.pop(key, None)
    env.update(env_extra)
    env["PYTHONPATH"] = str(python_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", _LAYOUT_PROBE],
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"probe failed (exit={result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    for line in result.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:"):])
    raise AssertionError(f"probe produced no report marker:\n{result.stdout}")


class ChaquopyLayoutTests(unittest.TestCase):
    """A + B. The exact Chaquopy environment, with and without the contract."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-appdir-")
        self.files_dir = Path(self.temp.name) / "files"
        self.files_dir.mkdir(parents=True)
        self.asset_app = _chaquopy_tree(self.files_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _probe(self, **env_extra: str) -> dict:
        # Chaquopy sets HOME to filesDir via os.environ.setdefault at startup.
        env = {"HOME": str(self.files_dir)}
        env.update(env_extra)
        return _probe(self.asset_app, env, self.files_dir)

    def test_kotlin_contract_pins_app_dir_to_files_dir(self) -> None:
        """The fix: ANDROID_PRIVATE=filesDir exported before Python.start()."""
        report = self._probe(ANDROID_PRIVATE=str(self.files_dir))
        self.assertEqual(report["app_dir"], str(self.files_dir))

    def test_every_kotlin_consumed_path_lands_under_files_dir(self) -> None:
        report = self._probe(ANDROID_PRIVATE=str(self.files_dir))
        expected = {
            # ZmuxTerminalActivity.launchLinuxSession / detectInstalledLinux
            "rootfs_dir": self.files_dir / "linux" / "rootfs",
            # TerminalSessionHelper.createLinuxSession binds this to /root
            "home_dir": self.files_dir / "home",
            "linuxenv_home_dir": self.files_dir / "home",
            # PROOT_TMP_DIR in createLinuxSession
            "cache_dir": self.files_dir / "cache",
            # ZmuxBackendLocator.candidateTokenPaths
            "token_file": self.files_dir / ".zmux_auth_token",
            "bin_dir": self.files_dir / "bin",
            "log_dir": self.files_dir / "logs",
        }
        for key, path in expected.items():
            self.assertEqual(report[key], str(path), key)
        self.assertNotIn("AssetFinder", json.dumps(report["rootfs_dir"]))

    def test_pure_chaquopy_without_the_contract_never_uses_the_asset_tree(self) -> None:
        """Safety net: even with no ANDROID_* at all, APP_DIR stays out of
        Chaquopy's extraction directory (it falls through to HOME=filesDir)."""
        report = self._probe()
        self.assertNotIn("AssetFinder", report["app_dir"])
        self.assertEqual(report["app_dir"], str(self.files_dir))

    def test_asset_tree_is_recognised_as_a_stale_app_dir_candidate(self) -> None:
        report = self._probe(ANDROID_PRIVATE=str(self.files_dir))
        self.assertIn(str(self.asset_app), report["legacy_candidates"])

    def test_contract_also_enables_the_wx_execve_guard(self) -> None:
        """Without ANDROID_PRIVATE, Chaquopy looked like a desktop runtime, so
        android_exec_blocked() was False and BIN_DIR was prepended to PATH."""
        self.assertTrue(self._probe(ANDROID_PRIVATE=str(self.files_dir))["blocked"])


class ResolverPriorityTests(unittest.TestCase):
    """A. resolve_app_dir() ordering, isolated from any package layout."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-appdir-resolver-")
        self.root = Path(self.temp.name)
        sys.path.insert(0, str(PYTHON_ROOT))
        os.environ.setdefault("ANDROID_PRIVATE", str(self.root / "import-time"))
        self.paths = importlib.import_module("zmux.paths")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _resolve(self, **env: str) -> Path:
        saved = {k: os.environ.get(k) for k in
                 ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH", "HOME")}
        try:
            for key in saved:
                os.environ.pop(key, None)
            os.environ.update(env)
            return self.paths.resolve_app_dir()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_android_private_beats_every_other_source(self) -> None:
        wanted = self.root / "files"
        home = self.root / "home"
        self.assertEqual(self._resolve(ANDROID_PRIVATE=str(wanted), HOME=str(home)), wanted)

    def test_android_argument_and_app_path_still_honoured_for_p4a(self) -> None:
        argument = self.root / "p4a-argument"
        app_path = self.root / "p4a-app-path"
        self.assertEqual(self._resolve(ANDROID_ARGUMENT=str(argument)), argument)
        self.assertEqual(self._resolve(ANDROID_APP_PATH=str(app_path)), app_path)

    def test_desktop_fallback_is_unchanged(self) -> None:
        """No ANDROID_*: the package root (where main.py lives) still wins, so
        desktop/CI keeps writing next to the sources."""
        self.assertEqual(self._resolve(HOME=str(self.root / "home")), PYTHON_ROOT)

    def test_chaquopy_asset_dir_is_classified_as_non_root(self) -> None:
        self.assertTrue(self.paths._is_chaquopy_asset_dir(
            Path("/data/user/0/com.zmux.terminal.debug/files/chaquopy/AssetFinder/app")))
        self.assertFalse(self.paths._is_chaquopy_asset_dir(
            Path("/data/user/0/com.zmux.terminal.debug/files")))


class LegacyInstallMigrationTests(unittest.TestCase):
    """C. Adopt (never re-download) an install made by a pre-alignment build."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-appdir-migrate-")
        self.root = Path(self.temp.name)
        sys.path.insert(0, str(PYTHON_ROOT))
        os.environ.setdefault("ANDROID_PRIVATE", str(self.root / "import-time"))
        self.linuxenv = importlib.import_module("zmux.linuxenv")
        self.paths = importlib.import_module("zmux.paths")

        self.files_dir = self.root / "files"
        self.legacy_app = self.files_dir / "chaquopy" / "AssetFinder" / "app"
        self.legacy_app.mkdir(parents=True)
        self.saved = (
            self.linuxenv._ROOTFS_DIR,
            self.linuxenv.HOME_DIR,
            self.paths.HOME_DIR,
            self.linuxenv.legacy_app_dir_candidates,
        )
        self.linuxenv._ROOTFS_DIR = self.files_dir / "linux" / "rootfs"
        self.linuxenv.HOME_DIR = self.files_dir / "home"
        self.linuxenv.legacy_app_dir_candidates = lambda: [self.legacy_app]

    def tearDown(self) -> None:
        (self.linuxenv._ROOTFS_DIR, self.linuxenv.HOME_DIR,
         self.paths.HOME_DIR, self.linuxenv.legacy_app_dir_candidates) = self.saved
        self.temp.cleanup()

    def _install_rootfs(self, target: Path, os_name: str = "alpine") -> Path:
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "busybox").write_text("busybox", encoding="utf-8")
        (target / "bin" / "sh").symlink_to("/bin/busybox")  # Alpine-style guest link
        (target / "etc").mkdir(parents=True)
        (target / "etc" / ".zmux-rootfs").write_text(f"{os_name}\n", encoding="utf-8")
        (target / "etc" / "alpine-release").write_text("3.22.5\n", encoding="utf-8")
        return target

    def test_stranded_rootfs_is_adopted_instead_of_re_downloaded(self) -> None:
        legacy_rootfs = self._install_rootfs(self.legacy_app / "linux" / "rootfs")
        self.assertFalse(self.linuxenv.is_installed())

        note = self.linuxenv.migrate_legacy_install()

        self.assertTrue(self.linuxenv.is_installed())
        self.assertEqual(self.linuxenv.installed_os(), "alpine")
        self.assertEqual(self.linuxenv.installed_version(), "3.22.5")
        self.assertFalse(legacy_rootfs.exists())
        self.assertIn(str(self.linuxenv.rootfs_dir()), note)

    def test_user_home_is_merged_without_overwriting(self) -> None:
        self._install_rootfs(self.legacy_app / "linux" / "rootfs")
        legacy_home = self.legacy_app / "home"
        (legacy_home / "projects" / "app").mkdir(parents=True)
        (legacy_home / "projects" / "app" / "main.py").write_text("old", encoding="utf-8")
        (legacy_home / ".profile").write_text("legacy profile", encoding="utf-8")

        live_home = self.linuxenv.HOME_DIR
        (live_home / "projects").mkdir(parents=True)
        (live_home / ".profile").write_text("live profile", encoding="utf-8")

        self.linuxenv.migrate_legacy_install()

        # Nested user work survives (projects/ exists on both sides)…
        self.assertEqual((live_home / "projects" / "app" / "main.py").read_text(), "old")
        self.assertFalse((legacy_home / "projects").exists())
        # …an existing live file is never clobbered…
        self.assertEqual((live_home / ".profile").read_text(), "live profile")
        # …and the losing copy is left on disk, never deleted behind the user's back.
        self.assertEqual((legacy_home / ".profile").read_text(), "legacy profile")

    def test_a_live_install_is_never_replaced_and_the_orphan_is_reported(self) -> None:
        self._install_rootfs(self.legacy_app / "linux" / "rootfs", "alpine")
        self._install_rootfs(self.linuxenv.rootfs_dir(), "alpine")
        (self.linuxenv.rootfs_dir() / "etc" / "keep-me").write_text("live", encoding="utf-8")

        note = self.linuxenv.migrate_legacy_install()

        self.assertTrue((self.linuxenv.rootfs_dir() / "etc" / "keep-me").is_file())
        self.assertTrue((self.legacy_app / "linux" / "rootfs" / "bin" / "busybox").is_file())
        self.assertIn("can be deleted to free space", note)

    def test_incomplete_legacy_directory_is_ignored_silently(self) -> None:
        (self.legacy_app / "linux" / "rootfs" / "etc").mkdir(parents=True)
        self.assertEqual(self.linuxenv.migrate_legacy_install(), "")
        self.assertFalse(self.linuxenv.is_installed())

    def test_nothing_to_migrate_is_a_silent_no_op(self) -> None:
        self.assertEqual(self.linuxenv.migrate_legacy_install(), "")
        self.assertEqual(self.linuxenv.migrate_legacy_install(), "")

    def test_migration_is_idempotent_after_a_successful_adoption(self) -> None:
        self._install_rootfs(self.legacy_app / "linux" / "rootfs")
        self.linuxenv.migrate_legacy_install()
        self.assertEqual(self.linuxenv.migrate_legacy_install(), "")
        self.assertTrue(self.linuxenv.is_installed())


class KotlinHostFileOwnershipTests(unittest.TestCase):
    """C. One directory, two writers — their filenames must never collide.

    Aligning APP_DIR with filesDir puts ``zmux.paths.ensure_cli_wrappers()``
    and ``TerminalSessionHelper.createLocalSession()`` in the *same*
    ``files/bin``. Before the alignment they were in different trees by
    accident, so nothing caught the overlap: both used to want the name
    ``linux-setup``, and the generated Python wrapper (``exec python -m
    zmux.cli``) would dead-end at "Python runtime not found on PATH" because
    Chaquopy ships no ``python`` executable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.helper_src = JAVA_HELPER.read_text(encoding="utf-8")
        sys.path.insert(0, str(PYTHON_ROOT))
        cls.registry = importlib.import_module("zmux.command_registry")

    def _host_written_bin_files(self) -> set[str]:
        return set(re.findall(r'new java\.io\.File\(binDir,\s*"([^"]+)"\)', self.helper_src))

    def test_host_bin_files_never_shadow_a_generated_wrapper(self) -> None:
        host_files = self._host_written_bin_files()
        self.assertTrue(host_files, "no host-written files/bin entries found — parser drifted")
        clash = host_files & set(self.registry.WRAPPER_COMMANDS)
        self.assertEqual(clash, set(), f"host script(s) shadowed by generated wrappers: {clash}")

    def test_bootstrap_alias_points_at_the_host_owned_script(self) -> None:
        alias = re.search(r"alias linux-setup='sh \" \+ shellQuote\(binDir\.getAbsolutePath\(\) \+ \"/([^\"]+)\"",
                          self.helper_src)
        self.assertIsNotNone(alias, "linux-setup alias no longer resolves through binDir")
        self.assertIn(alias.group(1), self._host_written_bin_files())
        self.assertNotIn(alias.group(1), self.registry.WRAPPER_COMMANDS)


class KotlinAppDirContractTests(unittest.TestCase):
    """D. Static contracts of the shipped activity — no hand-built paths."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = KOTLIN_ACTIVITY.read_text(encoding="utf-8")

    @staticmethod
    def _kotlin_block(src: str, signature: str) -> str:
        match = re.search(re.escape(signature) + r".*?(?=\n    (private|override|fun|@|/\*\*|companion))",
                          src, re.S)
        return match.group(0) if match else ""

    def test_no_string_literal_rebuilds_the_rootfs_path(self) -> None:
        offenders = re.findall(r'"[^"\n]*linux/rootfs[^"\n]*"', self.src)
        self.assertEqual(offenders, [], f"hard-coded rootfs literal(s): {offenders}")
        self.assertNotRegex(self.src, r'File\(\s*filesDir\s*,\s*"linux')
        self.assertNotRegex(self.src, r'File\(\s*filesDir\s*,\s*"home"\s*\)')

    def test_rootfs_and_home_come_from_linuxenv(self) -> None:
        self.assertIn('pythonPath("rootfs_dir")', self.src)
        self.assertIn('pythonPath("home_dir")', self.src)
        self.assertIn("module.callAttr(accessor)", self.src)
        self.assertIn('Python.getInstance().getModule("zmux.linuxenv")', self.src)

    def test_launch_and_autoreopen_both_ask_python_for_the_path(self) -> None:
        for signature in ("private fun launchLinuxSession(", "private fun detectInstalledLinux()"):
            block = self._kotlin_block(self.src, signature)
            self.assertTrue(block, f"could not locate {signature}")
            self.assertIn("installedRootfsDir()", block)
            self.assertIn("guestHomeDir()", block)
            self.assertNotIn("linux/rootfs", block)

    def test_launch_failure_reports_the_path_python_returned(self) -> None:
        block = self._kotlin_block(self.src, "private fun launchLinuxSession(")
        self.assertIn("rootfs?.absolutePath", block)
        self.assertNotIn("Rootfs path disappeared", block)
        self.assertIn("linux-setup", block)

    def test_android_private_is_exported_before_python_starts(self) -> None:
        export = 'Os.setenv("ANDROID_PRIVATE", filesDir.absolutePath, true)'
        self.assertIn(export, self.src, "the APP_DIR alignment contract is gone")
        setenv = self.src.index(export)
        # Python.start is launched off the UI thread (inside preparePythonRuntime)
        # so its `this` must be qualified as the activity.
        start = self.src.index("Python.start(AndroidPlatform(this@ZmuxTerminalActivity))")
        self.assertLess(setenv, start)
        self.assertLess(setenv, self.src.index('getModule("zmux'))
        on_create = self._kotlin_block(self.src, "override fun onCreate(")
        self.assertIn('Os.setenv("ANDROID_PRIVATE"', on_create)
        # The setenv must execute before the worker that starts Python is launched.
        self.assertIn("preparePythonRuntime()", on_create)
        self.assertLess(
            on_create.index('Os.setenv("ANDROID_PRIVATE"'),
            on_create.index("preparePythonRuntime()"),
        )

    def test_legacy_install_is_migrated_by_python_not_guessed_in_kotlin(self) -> None:
        self.assertIn('callAttr("migrate_legacy_install")', self.src)
        self.assertNotIn("AssetFinder\"", self.src)  # no Kotlin-side path guessing


if __name__ == "__main__":
    unittest.main(verbosity=2)
