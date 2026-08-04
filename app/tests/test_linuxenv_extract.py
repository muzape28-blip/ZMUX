"""Regression tests for the Python 3.14 tarfile-filter breakage of linux-setup.

Bug: on-device (p4a runtime is Python 3.14) `linux-setup` failed with
``./usr/bin/yes is a link to an absolute path``. Python 3.14 changed the
default ``TarFile.extractall()`` filter to ``"data"``, which rejects
absolute symlink targets — and the Alpine minirootfs is a busybox-style
tree with hundreds of them. Desktop/CI runtimes with an older default
(filter ``None``) never saw it, so this only exploded on the phone.

These tests run the REAL extraction path against REAL tarballs containing
absolute symlinks. They deliberately live outside ``test_linuxenv.py``
because that module carries a module-level skip guard tied to the proot
integration environment (ZMUX_PROOT_BIN/ZMUX_ROOTFS_DIR); the regression
must be exercised in plain CI.
"""
import hashlib
import io
import os
import runpy
import sys
import tarfile
import urllib.request
from pathlib import Path

import pytest

from zmux import linuxenv


def _build_busybox_tarball(target: Path) -> Path:
    """Create a mini busybox-style rootfs tarball with absolute symlinks.

    Mirrors the Alpine layout that broke on Python 3.14: real binaries in
    /bin, applet names in /usr/bin linked to the absolute /bin/busybox.
    """
    import time

    tarball = target / "minirootfs.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for directory in ("bin", "usr", "usr/bin", "etc"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = int(time.time())
            tf.addfile(info)
        busybox = tarfile.TarInfo("bin/busybox")
        payload = b"#!/bin/sh\n" * 16
        busybox.size = len(payload)
        busybox.mode = 0o755
        busybox.mtime = int(time.time())
        tf.addfile(busybox, io.BytesIO(payload))
        for applet in ("usr/bin/yes", "usr/bin/which", "bin/ash"):
            link = tarfile.TarInfo(applet)
            link.type = tarfile.SYMTYPE
            # The exact shape that made 3.14's data-filter bail out.
            link.linkname = "/bin/busybox"
            link.mtime = int(time.time())
            tf.addfile(link)
    return tarball


# ---------------------------------------------------------------------------
# Real extraction through the runtime's own tarfile
# ---------------------------------------------------------------------------
def test_safe_extract_extracts_absolute_symlinks(tmp_path):
    """End-to-end on THIS interpreter: absolute symlinks must survive.

    On runtimes with the filter kwarg (>=3.12 / patched 3.11) this proves
    the explicit filter="fully_trusted" call; on older runtimes it proves
    the TypeError fallback. Either way the applet links land on disk.
    """
    tarball = _build_busybox_tarball(tmp_path)
    target = tmp_path / "rootfs"
    target.mkdir()

    linuxenv._safe_extract(tarball, target)

    assert (target / "bin" / "busybox").is_file()
    for applet in ("usr/bin/yes", "usr/bin/which", "bin/ash"):
        link = target / applet
        assert link.is_symlink(), f"{applet} missing after extract"
        # Absolute target is preserved verbatim, not rebased or rejected.
        assert os.readlink(link) == "/bin/busybox"


def test_safe_extract_still_rejects_traversal(tmp_path):
    tarball = tmp_path / "evil.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        member = tarfile.TarInfo("../../escape.txt")
        member.size = 4
        tf.addfile(member, io.BytesIO(b"boom"))
    with pytest.raises(RuntimeError, match="unsafe"):
        linuxenv._safe_extract(tarball, tmp_path / "out")


def test_safe_extract_still_rejects_absolute_member_names(tmp_path):
    tarball = tmp_path / "evil.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        member = tarfile.TarInfo("/etc/pwned")
        member.size = 4
        tf.addfile(member, io.BytesIO(b"boom"))
    with pytest.raises(RuntimeError, match="unsafe"):
        linuxenv._safe_extract(tarball, tmp_path / "out")


def test_safe_extract_enforces_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(linuxenv, "MAX_ROOTFS_BYTES", 8)
    tarball = tmp_path / "big.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        member = tarfile.TarInfo("blob")
        member.size = 64
        tf.addfile(member, io.BytesIO(b"x" * 64))
    with pytest.raises(RuntimeError, match="safety limit"):
        linuxenv._safe_extract(tarball, tmp_path / "out")


# ---------------------------------------------------------------------------
# Version-specific behaviour: the 3.14 surface and the pre-3.12 surface
# ---------------------------------------------------------------------------
def test_safe_extract_requests_fully_trusted_filter(tmp_path, monkeypatch):
    """Simulate Python 3.14: default filter is "data" and refuses absolute
    link targets. _safe_extract must explicitly pass "fully_trusted" so the
    busybox rootfs still extracts — and must NOT swallow extract errors.
    """
    real_extractall = tarfile.TarFile.extractall
    seen_filters = []

    def py314_extractall(self, path, members=None, *,
                         numeric_owner=False, filter="data"):
        seen_filters.append(filter)
        if filter == "data":
            for member in members if members is not None else self.getmembers():
                if (member.issym() or member.islnk()) and member.linkname.startswith("/"):
                    raise RuntimeError(
                        f"{member.name}: link to an absolute path blocked by data filter"
                    )
        # Extraction itself is the interpreter's real implementation.
        return real_extractall(self, path, members)

    monkeypatch.setattr(tarfile.TarFile, "extractall", py314_extractall)

    tarball = _build_busybox_tarball(tmp_path)
    target = tmp_path / "rootfs"
    target.mkdir()
    linuxenv._safe_extract(tarball, target)

    assert seen_filters, "extractall was never called"
    assert seen_filters[0] == "fully_trusted"
    assert os.readlink(target / "usr/bin/yes") == "/bin/busybox"


def test_safe_extract_falls_back_without_filter_kwarg(tmp_path, monkeypatch):
    """Simulate an old runtime whose extractall() has no filter parameter:
    passing filter= must raise TypeError and _safe_extract must retry in
    the legacy form instead of dying (the original 3.11 behaviour)."""
    real_extractall = tarfile.TarFile.extractall
    calls = []

    def legacy_extractall(self, path, members=None, **kwargs):
        calls.append(kwargs)
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real_extractall(self, path, members)

    monkeypatch.setattr(tarfile.TarFile, "extractall", legacy_extractall)

    tarball = _build_busybox_tarball(tmp_path)
    target = tmp_path / "rootfs"
    target.mkdir()
    linuxenv._safe_extract(tarball, target)

    assert calls[0] == {"filter": "fully_trusted"}  # tried the modern form
    assert calls[1] == {}                            # then the legacy form
    assert os.readlink(target / "usr/bin/yes") == "/bin/busybox"


# ---------------------------------------------------------------------------
# linux-setup idempotency (no network may be touched on the second run)
# ---------------------------------------------------------------------------
def _seed_installed_rootfs(root: Path) -> None:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_text("#!/bin/sh\n")
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "alpine-release").write_text("3.22.5\n")


def test_install_short_circuits_when_already_installed(tmp_path, monkeypatch):
    rootfs = tmp_path / "rootfs"
    _seed_installed_rootfs(rootfs)
    monkeypatch.setattr(linuxenv, "_ROOTFS_DIR", rootfs)

    def forbidden_urlopen(*args, **kwargs):  # any download attempt is a bug
        raise AssertionError("install() touched the network despite being installed")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    first = linuxenv.install()
    second = linuxenv.install()
    assert first["ok"] and first["already"] and first["version"] == "3.22.5"
    assert second["ok"] and second["already"]


def test_install_end_to_end_from_local_mirror(tmp_path, monkeypatch):
    """Full install pipeline against a file:// mirror: download, SHA-512
    verify, extract a rootfs with absolute symlinks, bootstrap, swap live —
    then prove the second invocation is a no-op. No mocks in the pipeline:
    the bytes really move through urllib -> sha512 -> tarfile -> disk."""
    arch = linuxenv.alpine_arch()

    mirror = tmp_path / "mirror"
    release_dir = mirror / linuxenv.ALPINE_BRANCH / "releases" / arch
    release_dir.mkdir(parents=True)
    staged = _build_busybox_tarball(release_dir)
    tarball_name = f"alpine-minirootfs-{linuxenv.ALPINE_VERSION}-{arch}.tar.gz"
    (release_dir / tarball_name).write_bytes(staged.read_bytes())
    staged.unlink()

    monkeypatch.setattr(linuxenv, "ALPINE_MIRROR", mirror.as_uri())
    monkeypatch.setattr(
        linuxenv, "ALPINE_SHA512",
        {arch: hashlib.sha512((release_dir / tarball_name).read_bytes()).hexdigest()},
    )
    monkeypatch.setattr(linuxenv, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(linuxenv, "_ROOTFS_DIR", tmp_path / "live" / "rootfs")
    monkeypatch.setattr(linuxenv, "_STAGING_DIR", tmp_path / "live" / ".staging")

    progress_lines = []
    result = linuxenv.install(progress=progress_lines.append)
    assert result["ok"] and not result["already"]
    assert linuxenv.is_installed()
    # The busybox applet symlink that broke Python 3.14 is on disk, absolute.
    assert os.readlink(Path(result["path"]) / "usr/bin" / "yes") == "/bin/busybox"
    # Progress actually streamed (the "everything called must answer" rule).
    assert any("checksum verified" in line for line in progress_lines)
    # Staging area never leaks into the live path.
    assert not (tmp_path / "live" / ".staging").exists()

    def forbidden_urlopen(*args, **kwargs):
        raise AssertionError("second install() re-downloaded the rootfs")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    again = linuxenv.install()
    assert again["ok"] and again["already"]


def test_module_has_no_other_extractall_call_sites():
    """Guard against a second unfiltered extractall sneaking back in."""
    source = (Path(__file__).parent.parent / "zmux" / "linuxenv.py").read_text()
    assert source.count("archive.extractall(") == 2  # modern + legacy fallback
    assert 'filter="fully_trusted"' in source
