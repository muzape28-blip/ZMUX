#!/usr/bin/env python3
"""Regression tests for the rootfs installer code usable without Android/PRoot.

These tests deliberately exercise the conditions which were hidden by the old
Chaquopy ``NoneType: None`` message: a Java-style progress object, the valid
Debian asset pin, and proot-distro's top-level archive directory.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PROJECT_ROOT / "app" / "src" / "main" / "python"

# paths.py resolves runtime files at import time. Keep that state in a temporary
# app-private-looking path rather than modifying the source tree during tests.
_TEST_APP_DIR = tempfile.TemporaryDirectory(prefix="zmux-linuxenv-test-app-")
os.environ.setdefault("ANDROID_PRIVATE", _TEST_APP_DIR.name)
sys.path.insert(0, str(PYTHON_ROOT))
linuxenv = importlib.import_module("zmux.linuxenv")


class InvokeOnlyCallback:
    """Matches the public Java ProgressCallback used by the Kotlin activity."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def invoke(self, message: str) -> None:
        self.messages.append(message)


class LinuxEnvInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-linuxenv-test-")
        self.root = Path(self.temp.name)
        self.old_root = linuxenv._ROOTFS_DIR
        self.old_staging = linuxenv._STAGING_DIR
        self.old_cache = linuxenv.CACHE_DIR
        linuxenv._ROOTFS_DIR = self.root / "installed-rootfs"
        linuxenv._STAGING_DIR = self.root / "staging-rootfs"
        linuxenv.CACHE_DIR = self.root / "cache"

    def tearDown(self) -> None:
        linuxenv._ROOTFS_DIR = self.old_root
        linuxenv._STAGING_DIR = self.old_staging
        linuxenv.CACHE_DIR = self.old_cache
        self.temp.cleanup()

    def _tar(self, name: str, entries: dict[str, bytes]) -> Path:
        archive_path = self.root / name
        payload = self.root / "payload"
        payload.mkdir(exist_ok=True)
        for relative, content in entries.items():
            file_path = payload / relative
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in sorted(payload.rglob("*")):
                archive.add(path, arcname=path.relative_to(payload).as_posix())
        return archive_path

    def test_java_style_progress_callback_is_not_treated_as_python_callable(self) -> None:
        callback = InvokeOnlyCallback()
        linuxenv._emit_progress(callback, "Downloading rootfs\n")
        self.assertEqual(callback.messages, ["Downloading rootfs\r\n"])

    def test_debian_spec_uses_existing_verified_termux_asset_and_native_arch(self) -> None:
        spec = linuxenv._rootfs_spec("debian")
        self.assertEqual(spec["checksum_name"], "sha256")
        self.assertTrue(str(spec["checksum"]))
        self.assertIn("debian-bookworm-", str(spec["url"]))
        self.assertIn("v4.7.0", str(spec["url"]))
        self.assertNotIn("v4.0.0", str(spec["url"]))
        if linuxenv.alpine_arch() == "x86_64":
            self.assertIn("debian-bookworm-x86_64", str(spec["url"]))

    def test_install_accepts_java_style_progress_callback_end_to_end(self) -> None:
        archive = self._tar(
            "alpine-rootfs.tar.gz",
            {
                "bin/sh": b"#!/bin/sh\n",
                "etc/alpine-release": b"3.22.5\n",
            },
        )
        payload = archive.read_bytes()
        spec = {
            "os_name": "alpine",
            "label": "Alpine test",
            "url": "https://example.invalid/alpine-rootfs.tar.gz",
            "checksum_name": "sha512",
            "checksum": hashlib.sha512(payload).hexdigest(),
            "extension": "tar.gz",
            "strip_components": 0,
        }

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        callback = InvokeOnlyCallback()
        with patch.object(linuxenv, "_rootfs_spec", return_value=spec), patch.object(
            linuxenv.urllib.request, "urlopen", return_value=Response(payload)
        ):
            result = linuxenv.install(callback, "alpine")

        self.assertTrue(result["ok"])
        self.assertFalse(result["already"])
        self.assertEqual(linuxenv.installed_os(), "alpine")
        self.assertTrue(any("Downloading Alpine test" in message for message in callback.messages))
        self.assertTrue(any("checksum verified" in message for message in callback.messages))

    def test_debian_archive_top_directory_is_stripped_and_detected(self) -> None:
        archive = self._tar(
            "debian-rootfs.tar.gz",
            {
                "debian-bookworm-aarch64/bin/sh": b"#!/bin/sh\n",
                "debian-bookworm-aarch64/etc/debian_version": b"12.9\n",
            },
        )
        staging = self.root / "staging"
        staging.mkdir()
        linuxenv._safe_extract(archive, staging, strip_components=1)
        self.assertTrue((staging / "bin" / "sh").is_file())
        self.assertTrue((staging / "etc" / "debian_version").is_file())
        self.assertFalse((staging / "debian-bookworm-aarch64").exists())

        linuxenv._bootstrap(staging, "debian")
        staging.replace(linuxenv.rootfs_dir())
        self.assertEqual(linuxenv.installed_os(), "debian")
        self.assertEqual(linuxenv.installed_version(), "12.9")
        self.assertTrue(linuxenv.is_installed())

    def test_alpine_absolute_guest_sh_link_is_valid(self) -> None:
        """Alpine uses /bin/sh -> /bin/busybox, not a host-side link."""
        archive = self.root / "alpine-absolute-sh.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            busybox = tarfile.TarInfo("bin/busybox")
            busybox.size = len(b"busybox")
            busybox.mode = 0o755
            handle.addfile(busybox, io.BytesIO(b"busybox"))

            shell = tarfile.TarInfo("bin/sh")
            shell.type = tarfile.SYMTYPE
            shell.linkname = "/bin/busybox"
            handle.addfile(shell)

            release = tarfile.TarInfo("etc/alpine-release")
            release.size = len(b"3.22.5\n")
            handle.addfile(release, io.BytesIO(b"3.22.5\n"))

        staging = self.root / "alpine-staging"
        staging.mkdir()
        linuxenv._safe_extract(archive, staging)
        self.assertTrue(linuxenv._guest_regular_file(staging, "/bin/sh"))
        linuxenv._bootstrap(staging, "alpine")
        staging.replace(linuxenv.rootfs_dir())
        self.assertEqual(linuxenv.installed_os(), "alpine")
        self.assertEqual(linuxenv.installed_version(), "3.22.5")

    def test_skips_root_metadata_and_android_forbidden_dev_nodes(self) -> None:
        """Real rootfs archives contain both of these entries on-device."""
        archive = self.root / "rootfs-with-dev.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            parent = tarfile.TarInfo("..")
            parent.type = tarfile.DIRTYPE
            handle.addfile(parent)

            dev_dir = tarfile.TarInfo("dev")
            dev_dir.type = tarfile.DIRTYPE
            dev_dir.mode = 0o755
            handle.addfile(dev_dir)

            null = tarfile.TarInfo("dev/null")
            null.type = tarfile.CHRTYPE
            null.devmajor = 1
            null.devminor = 3
            handle.addfile(null)

            shell = tarfile.TarInfo("bin/sh")
            shell.size = len(b"#!/bin/sh\n")
            shell.mode = 0o755
            handle.addfile(shell, io.BytesIO(b"#!/bin/sh\n"))

        target = self.root / "target"
        target.mkdir()
        linuxenv._safe_extract(archive, target)
        self.assertTrue((target / "bin" / "sh").is_file())
        self.assertFalse((target / "dev" / "null").exists())

    def test_rejects_unsafe_archive_member(self) -> None:
        archive = self.root / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            info = tarfile.TarInfo("../outside")
            info.size = 1
            import io
            handle.addfile(info, io.BytesIO(b"x"))
        target = self.root / "target"
        target.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unsafe archive member"):
            linuxenv._safe_extract(archive, target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
