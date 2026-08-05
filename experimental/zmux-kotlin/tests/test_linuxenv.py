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
        self.old_native_library_dir = linuxenv._NATIVE_LIBRARY_DIR_OVERRIDE
        linuxenv._ROOTFS_DIR = self.root / "installed-rootfs"
        linuxenv._STAGING_DIR = self.root / "staging-rootfs"
        linuxenv.CACHE_DIR = self.root / "cache"

    def tearDown(self) -> None:
        linuxenv._ROOTFS_DIR = self.old_root
        linuxenv._STAGING_DIR = self.old_staging
        linuxenv.CACHE_DIR = self.old_cache
        linuxenv._NATIVE_LIBRARY_DIR_OVERRIDE = self.old_native_library_dir
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

    def test_kotlin_native_library_override_finds_packaged_proot(self) -> None:
        native_dir = self.root / "native-libs"
        native_dir.mkdir()
        proot = native_dir / "libproot.so"
        proot.write_bytes(b"not-an-elf-test-placeholder")
        proot.chmod(0o755)

        self.assertTrue(linuxenv.set_native_library_dir(str(native_dir)))
        self.assertEqual(linuxenv.native_library_dir(), str(native_dir))
        self.assertEqual(linuxenv.proot_binary(), str(proot))

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


class LegacyRootfsAdoptionTests(unittest.TestCase):
    """Pre-fix builds left a complete rootfs inside the Chaquopy AssetFinder
    dir; upgrading must adopt it instead of forcing a ~100 MiB redownload."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-adopt-test-")
        self.root = Path(self.temp.name)
        # The legacy layout: <package_root>/linux/rootfs derived from __file__.
        self.legacy_pkg_root = self.root / "chaquopy" / "AssetFinder" / "app"
        self.legacy_rootfs = self.legacy_pkg_root / "linux" / "rootfs"
        (self.legacy_rootfs / "bin").mkdir(parents=True)
        (self.legacy_rootfs / "bin" / "sh").write_bytes(b"#!/bin/sh\n")
        (self.legacy_rootfs / "etc").mkdir()
        (self.legacy_rootfs / "etc" / "alpine-release").write_text("3.22.5\n")
        # The new, post-fix install location.
        self.new_rootfs = self.root / "filesdir" / "linux" / "rootfs"
        self.old_rootfs_dir = linuxenv._ROOTFS_DIR
        linuxenv._ROOTFS_DIR = self.new_rootfs
        self._android_patch = patch.object(sys, "getandroidapilevel", lambda: 35, create=True)
        self._android_patch.start()

    def tearDown(self) -> None:
        self._android_patch.stop()
        linuxenv._ROOTFS_DIR = self.old_rootfs_dir
        self.temp.cleanup()

    def _call(self) -> None:
        fake_module_file = self.legacy_pkg_root / "zmux" / "linuxenv.py"
        with patch.object(linuxenv, "__file__", str(fake_module_file)):
            linuxenv._adopt_legacy_rootfs()

    def test_adopts_complete_legacy_rootfs(self) -> None:
        self._call()
        self.assertFalse(self.legacy_rootfs.exists())
        self.assertTrue((self.new_rootfs / "bin" / "sh").is_file())
        self.assertEqual(linuxenv.installed_os(), "alpine")

    def test_skips_when_new_rootfs_already_exists(self) -> None:
        self.new_rootfs.mkdir(parents=True)
        (self.new_rootfs / "newer").write_text("x")
        self._call()
        self.assertTrue(self.legacy_rootfs.is_dir())  # untouched
        self.assertEqual((self.new_rootfs / "newer").read_text(), "x")

    def test_skips_incomplete_legacy_rootfs(self) -> None:
        (self.legacy_rootfs / "bin" / "sh").unlink()
        self._call()
        self.assertTrue(self.legacy_rootfs.is_dir())
        self.assertFalse(self.new_rootfs.exists())

    def test_never_touches_explicit_rootfs_override(self) -> None:
        with patch.dict(os.environ, {"ZMUX_ROOTFS_DIR": str(self.new_rootfs)}):
            self._call()
        self.assertTrue(self.legacy_rootfs.is_dir())
        self.assertFalse(self.new_rootfs.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
