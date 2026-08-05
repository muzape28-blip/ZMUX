#!/usr/bin/env python3
"""Regression tests for APP_DIR resolution under embedded Android runtimes.

The "Rootfs path disappeared before PRoot launch" incident: under Chaquopy
none of the python-for-android ANDROID_* variables are set, so paths.py fell
back to the directory containing the Python package — Chaquopy's AssetFinder
extraction dir (files/chaquopy/AssetFinder/app) — while the Kotlin terminal
looked in filesDir/linux/rootfs. The install succeeded and the launch check
then reported the rootfs as missing.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PROJECT_ROOT / "app" / "src" / "main" / "python"

# paths.py resolves runtime files at import time. Keep that state in a
# temporary app-private-looking path rather than modifying the source tree.
_TEST_APP_DIR = tempfile.TemporaryDirectory(prefix="zmux-paths-test-app-")
os.environ.setdefault("ANDROID_PRIVATE", _TEST_APP_DIR.name)
sys.path.insert(0, str(PYTHON_ROOT))
paths = importlib.import_module("zmux.paths")


class _AndroidMarker:
    """Patch sys.getandroidapilevel into existence for the scope of a test."""

    def __enter__(self):
        self._patch = patch.object(sys, "getandroidapilevel", lambda: 35, create=True)
        self._patch.__enter__()
        return self

    def __exit__(self, *exc_info):
        self._patch.__exit__(*exc_info)
        return False


class ResolveAppDirTests(unittest.TestCase):
    def _android_free_env(self, **overrides) -> dict:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "ZMUX_APP_DIR",
                "ANDROID_PRIVATE",
                "ANDROID_ARGUMENT",
                "ANDROID_APP_PATH",
            )
        }
        env.update(overrides)
        return env

    def test_host_supplied_zmux_app_dir_wins_over_everything(self) -> None:
        with tempfile.TemporaryDirectory() as host_dir, tempfile.TemporaryDirectory() as other:
            env = self._android_free_env(ZMUX_APP_DIR=host_dir, ANDROID_PRIVATE=other)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(paths.resolve_app_dir(), Path(host_dir))

    def test_invalid_zmux_app_dir_falls_through_to_p4a_vars(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            env = self._android_free_env(
                ZMUX_APP_DIR="/nonexistent/zmux-app-dir/that/cannot/be/created",
                ANDROID_PRIVATE=other,
            )
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(paths.resolve_app_dir(), Path(other))

    def test_chaquopy_home_pin_used_when_p4a_vars_absent(self) -> None:
        # HOME is what Chaquopy pins to Context.getFilesDir() on device.
        with tempfile.TemporaryDirectory() as files_dir:
            env = self._android_free_env(HOME=files_dir)
            with patch.dict(os.environ, env, clear=True), _AndroidMarker():
                self.assertEqual(paths.resolve_app_dir(), Path(files_dir))

    def test_desktop_resolution_ignores_home(self) -> None:
        # Without getandroidapilevel (plain desktop CPython), HOME must NOT be
        # used as APP_DIR; the package's project root remains authoritative.
        with tempfile.TemporaryDirectory() as fake_home:
            env = self._android_free_env(HOME=fake_home)
            with patch.dict(os.environ, env, clear=True):
                resolved = paths.resolve_app_dir()
            self.assertNotEqual(resolved, Path(fake_home))
            self.assertTrue((resolved / "zmux").is_dir() or resolved == Path(paths.__file__).resolve().parent.parent)

    def test_unwritable_home_falls_through(self) -> None:
        env = self._android_free_env(HOME="/nonexistent/zmux-home/that/cannot/be/created")
        with patch.dict(os.environ, env, clear=True), _AndroidMarker():
            resolved = paths.resolve_app_dir()
        self.assertNotEqual(str(resolved), "/nonexistent/zmux-home/that/cannot/be/created")


if __name__ == "__main__":
    unittest.main()
