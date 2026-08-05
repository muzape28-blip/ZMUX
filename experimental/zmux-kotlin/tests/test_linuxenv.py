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


class GuestPromptBrandingTests(unittest.TestCase):
    """Distro-branded login prompt proven per guest shell:
    zmux@alpine:~$ (busybox ash expands \w) / zmux@debian:$ (dash has no \w)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-prompt-test-")
        self.root = Path(self.temp.name)
        self.old_root = linuxenv._ROOTFS_DIR
        self.old_home = linuxenv.HOME_DIR
        linuxenv._ROOTFS_DIR = self.root / "installed-rootfs"
        linuxenv.HOME_DIR = self.root / "home"

    def tearDown(self) -> None:
        linuxenv._ROOTFS_DIR = self.old_root
        linuxenv.HOME_DIR = self.old_home
        self.temp.cleanup()

    def _guest(self, os_name: str, with_profile: bool = True) -> Path:
        root = self.root / f"{os_name}-guest"
        if root.exists():
            import shutil as _sh
            _sh.rmtree(root)
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "sh").write_bytes(b"#!/bin/sh\n")
        (root / "etc").mkdir()
        if os_name == "alpine":
            (root / "etc" / "alpine-release").write_text("3.22.5\n")
        else:
            (root / "etc" / "debian_version").write_text("12.4\n")
        if with_profile:
            # The real distro default users kept seeing after login.
            (root / "etc" / "profile").write_text("export PS1='\\h:\\w\\$ '\n")
        return root

    def test_bootstrap_brands_alpine_prompt_and_stays_idempotent(self) -> None:
        root = self._guest("alpine")
        linuxenv._bootstrap(root, "alpine")
        profile = (root / "etc" / "profile").read_text()
        self.assertIn("export PS1='zmux@alpine:\\w$ '", profile)
        self.assertIn("export PS1='\\h:\\w\\$ '", profile)  # distro default kept
        linuxenv._bootstrap(root, "alpine")
        self.assertEqual(profile, (root / "etc" / "profile").read_text())  # no dupes

    def test_bootstrap_brands_debian_prompt_dash_safe(self) -> None:
        root = self._guest("debian")
        linuxenv._bootstrap(root, "debian")
        profile = (root / "etc" / "profile").read_text()
        # dash has NO \w escape: the Debian brand must be escape-free, or the
        # prompt renders the literal "\w" seen on-device.
        self.assertIn("export PS1='zmux@debian:$ '", profile)
        self.assertNotIn("zmux@debian:\\w", profile)
        self.assertNotIn("zmux@alpine", profile)

    def test_branding_creates_missing_profile(self) -> None:
        root = self._guest("alpine", with_profile=False)
        (root / "etc" / "profile").unlink(missing_ok=True)
        linuxenv._install_guest_prompt(root, "alpine")
        self.assertIn("zmux@alpine:\\w$ ", (root / "etc" / "profile").read_text())

    def test_ensure_guest_prompt_repairs_already_installed_rootfs(self) -> None:
        # Simulates "already installed" installs: install() returns early and
        # never reruns _bootstrap, so the launch path must brand on its own.
        root = self._guest("debian")
        import shutil as _sh
        _sh.copytree(root, linuxenv._ROOTFS_DIR)
        profile = linuxenv._ROOTFS_DIR / "etc" / "profile"
        self.assertNotIn("ZMUX_PS1", profile.read_text())
        self.assertTrue(linuxenv.ensure_guest_prompt())
        self.assertIn("export PS1='zmux@debian:$ '", profile.read_text())
        before = profile.read_text()
        linuxenv.ensure_guest_prompt()  # idempotent
        self.assertEqual(before, profile.read_text())

    def test_ensure_guest_prompt_rewrites_stale_v1_brand_in_place(self) -> None:
        # Rootfs branded by the FIRST branding build carries the
        # dash-broken \w line; every launch must upgrade it in place.
        root = self._guest("debian")
        import shutil as _sh
        _sh.copytree(root, linuxenv._ROOTFS_DIR)
        profile = linuxenv._ROOTFS_DIR / "etc" / "profile"
        stale = (
            profile.read_text()
            + "\n# ZMUX_PS1: distro-branded ZMUX guest prompt\n"
            + "export PS1='zmux@debian:\\w$ '\n"
        )
        profile.write_text(stale)
        linuxenv.ensure_guest_prompt()
        content = profile.read_text()
        self.assertIn("export PS1='zmux@debian:$ '", content)
        self.assertNotIn("export PS1='zmux@debian:\\w$ '", content)
        self.assertEqual(content.count("ZMUX_PS1"), 1)  # marker not duplicated
        self.assertIn("export PS1='\\h:\\w\\$ '", content)  # distro default kept

    def test_ensure_guest_prompt_alpine_brand_stays_busybox_expanded(self) -> None:
        # Alpine is PROVEN working on-device with \w; never regress it.
        root = self._guest("alpine")
        import shutil as _sh
        _sh.copytree(root, linuxenv._ROOTFS_DIR)
        linuxenv.ensure_guest_prompt()
        profile = linuxenv._ROOTFS_DIR / "etc" / "profile"
        self.assertIn("export PS1='zmux@alpine:\\w$ '", profile.read_text())

    def test_ensure_guest_prompt_noop_without_install(self) -> None:
        self.assertFalse(linuxenv.ensure_guest_prompt())

    def test_home_layout_writes_distro_aware_shell_proven_ps1(self) -> None:
        linuxenv.ensure_user_home_layout()
        content = (linuxenv.HOME_DIR / ".profile").read_text()
        self.assertIn("zmux@alpine:\\w$ ", content)  # busybox ash expands this
        self.assertIn("zmux@debian:$ ", content)     # dash-safe: no \w
        self.assertNotIn("zmux@debian:\\w", content)
        self.assertNotIn("\\\\w", content)  # no literal double backslash

    def test_home_layout_migrates_v1_branding_lines(self) -> None:
        # ~/.profile shipped by the FIRST branding build: Debian branch had \w.
        profile = linuxenv.HOME_DIR / ".profile"
        profile.parent.mkdir(parents=True, exist_ok=True)
        v1_block = (
            "if [ -f /etc/alpine-release ]; then\n"
            "  export PS1='zmux@alpine:\\w$ '\n"
            "elif [ -f /etc/debian_version ]; then\n"
            "  export PS1='zmux@debian:\\w$ '\n"
            "else\n"
            "  export PS1='zmux@linux:\\w$ '\n"
            "fi\n"
        )
        profile.write_text("# default from an older build\n" + v1_block)
        linuxenv.ensure_user_home_layout()
        content = profile.read_text()
        self.assertIn("export PS1='zmux@debian:$ '", content)
        self.assertIn("export PS1='zmux@linux:$ '", content)
        self.assertIn("export PS1='zmux@alpine:\\w$ '", content)  # alpine untouched
        self.assertIn("# default from an older build", content)  # user bytes kept
        # Migration is idempotent.
        linuxenv.ensure_user_home_layout()
        self.assertEqual(content, profile.read_text())

    def test_home_layout_migrates_exact_legacy_ps1_lines(self) -> None:
        profile = linuxenv.HOME_DIR / ".profile"
        profile.parent.mkdir(parents=True, exist_ok=True)
        for stale in linuxenv._LEGACY_PROFILE_PS1_LINES:
            profile.write_text(f"# default from an older build\n{stale}\nmkdir -p \"$HOME/projects\"\n")
            linuxenv.ensure_user_home_layout()
            content = profile.read_text()
            self.assertIn(linuxenv._GUEST_PS1_BLOCK, content)
            # Anchored at a line start: the indented alpine line inside the
            # replacement block is fine, only a surviving top-level default is not.
            self.assertNotIn("\n" + stale, "\n" + content)

    def test_home_layout_never_touches_custom_ps1(self) -> None:
        profile = linuxenv.HOME_DIR / ".profile"
        profile.parent.mkdir(parents=True, exist_ok=True)
        custom = "# mine\nexport PS1='user> '\n"
        profile.write_text(custom)
        linuxenv.ensure_user_home_layout()
        self.assertEqual(custom, profile.read_text())

    def test_interactive_env_ps1_follows_installed_os(self) -> None:
        root = self._guest("debian")
        import shutil as _sh
        _sh.copytree(root, linuxenv._ROOTFS_DIR)
        env = linuxenv.interactive_env()
        self.assertEqual(env["PS1"], "zmux@debian:$ ")

    def test_interactive_env_ps1_alpine_keeps_busybox_escape(self) -> None:
        root = self._guest("alpine")
        import shutil as _sh
        _sh.copytree(root, linuxenv._ROOTFS_DIR)
        env = linuxenv.interactive_env()
        self.assertEqual(env["PS1"], "zmux@alpine:\\w$ ")


class GuestDnsTests(unittest.TestCase):
    """proot-distro tarballs ship the *build host's* resolv.conf (GitHub
    Actions runners: the systemd-resolved stub 127.0.0.53). ZMUX must always
    normalise it — that poisoned file is why apt said 'Temporary failure
    resolving' while the identical Alpine flow resolved fine."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmux-dns-test-")
        self.root = Path(self.temp.name)
        self.old_root = linuxenv._ROOTFS_DIR
        linuxenv._ROOTFS_DIR = self.root / "installed-rootfs"

    def tearDown(self) -> None:
        linuxenv._ROOTFS_DIR = self.old_root
        self.temp.cleanup()

    def _guest_root(self, os_name: str = "debian") -> Path:
        root = self.root / f"{os_name}-guest"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "sh").write_bytes(b"#!/bin/sh\n")
        (root / "etc").mkdir(parents=True)
        if os_name == "alpine":
            (root / "etc" / "alpine-release").write_text("3.22.5\n")
        else:
            (root / "etc" / "debian_version").write_text("12.4\n")
        return root

    def test_bootstrap_overwrites_poisoned_tarball_resolv(self) -> None:
        root = self._guest_root("debian")
        (root / "etc" / "resolv.conf").write_text(
            "nameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n"
        )
        good = "nameserver 8.8.8.8\nnameserver 1.1.1.1\n"
        with patch.object(linuxenv, "_guest_resolv_conf_content", lambda: good):
            linuxenv._bootstrap(root, "debian")
        content = (root / "etc" / "resolv.conf").read_text()
        self.assertEqual(content, good)
        self.assertNotIn("127.0.0.53", content)

    def test_bootstrap_replaces_symlinked_resolv_with_regular_file(self) -> None:
        root = self._guest_root("debian")
        # systemd-resolved style: a dangling symlink into /run.
        (root / "etc" / "resolv.conf").symlink_to("/run/systemd/resolve/stub-resolv.conf")
        with patch.object(
            linuxenv, "_guest_resolv_conf_content",
            lambda: "nameserver 8.8.8.8\nnameserver 1.1.1.1\n",
        ):
            linuxenv._bootstrap(root, "debian")
        resolv = root / "etc" / "resolv.conf"
        self.assertFalse(resolv.is_symlink())
        self.assertTrue(resolv.is_file())
        self.assertIn("nameserver 8.8.8.8", resolv.read_text())

    def test_alpine_resolv_bytes_stay_identical(self) -> None:
        # Alpine shipped NO resolv.conf, so the old fallback wrote exactly
        # these bytes and it works on-device. Protect that byte-identity.
        root = self._guest_root("alpine")
        good = "nameserver 8.8.8.8\nnameserver 1.1.1.1\n"
        with patch.object(linuxenv, "_guest_resolv_conf_content", lambda: good):
            linuxenv._bootstrap(root, "alpine")
        self.assertEqual((root / "etc" / "resolv.conf").read_text(), good)

    def test_install_already_installed_heals_dns_without_redownload(self) -> None:
        root = linuxenv._ROOTFS_DIR
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "sh").write_bytes(b"#!/bin/sh\n")
        (root / "etc").mkdir(parents=True)
        (root / "etc" / "debian_version").write_text("12.4\n")
        (root / "etc" / ".zmux-rootfs").write_text("debian\n")
        (root / "etc" / "resolv.conf").write_text("nameserver 127.0.0.53\n")
        with patch.object(
            linuxenv, "_guest_resolv_conf_content",
            lambda: "nameserver 8.8.8.8\nnameserver 1.1.1.1\n",
        ):
            result = linuxenv.install(progress=None, os_name="debian")
        self.assertTrue(result["already"])
        self.assertNotIn("127.0.0.53", (root / "etc" / "resolv.conf").read_text())
        self.assertIn("nameserver 8.8.8.8", (root / "etc" / "resolv.conf").read_text())

    def test_guest_resolv_conf_content_prefers_wellformed_host_nameservers(self) -> None:
        host = self.root / "host-resolv.conf"
        host.write_text(
            "# generated\nnameserver 9.9.9.9\n"
            "search lan\noptions edns0\nnameserver 1.0.0.1 extra-junk\n"
            "nameserver 1.1.1.1\n"
        )
        content = linuxenv._guest_resolv_conf_content(host)
        self.assertEqual(content, "nameserver 9.9.9.9\nnameserver 1.1.1.1\n")
        # Unusable host file -> public fallback.
        host.write_text("# nothing\nsearch lan\n")
        self.assertEqual(
            linuxenv._guest_resolv_conf_content(host),
            "nameserver 8.8.8.8\nnameserver 1.1.1.1\n",
        )
        # Missing host file -> public fallback (this is the Android case).
        self.assertEqual(
            linuxenv._guest_resolv_conf_content(self.root / "no-such-file"),
            "nameserver 8.8.8.8\nnameserver 1.1.1.1\n",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
