"""Tests for the proot'd Alpine userland (zmux.linuxenv) and its shell wiring.

Two layers are tested:

1. Pure logic — architecture mapping, guest-path mapping, safe extraction,
   bootstrap files, command-line construction. These run everywhere.
2. Integration — real proot + real Alpine rootfs, exactly like the terminal
   runs them. These are skipped unless the caller provides
   ``ZMUX_PROOT_BIN`` (an executable proot) and ``ZMUX_ROOTFS_DIR`` (an
   extracted Alpine rootfs); the development harness sets both.
"""
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from zmux import linuxenv
from zmux.python_shell import PythonShell


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------
def test_alpine_arch_is_supported():
    assert linuxenv.alpine_arch() in {"x86_64", "aarch64", "armv7"}


def test_rootfs_state_matches_environment():
    # When the harness points at a real rootfs (ZMUX_ROOTFS_DIR), it must be
    # reported installed; otherwise the default app dir has none.
    real = os.environ.get("ZMUX_ROOTFS_DIR")
    expected = bool(real and Path(real).is_dir())
    assert linuxenv.is_installed() is expected


def test_guest_cwd_maps_home_to_root():
    home = linuxenv.HOME_DIR.resolve()
    assert linuxenv.guest_cwd(home) == "/root"
    sub = home / "projects" / "demo"
    assert linuxenv.guest_cwd(sub) == "/root/projects/demo"


def test_guest_cwd_falls_back_outside_home():
    assert linuxenv.guest_cwd(Path("/somewhere/else")) == "/"


def test_interactive_environment_creates_persistent_workspace_profile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(linuxenv, "HOME_DIR", home)
    env = linuxenv.interactive_env()
    profile = home / ".profile"
    assert (home / "projects").is_dir()
    assert profile.is_file()
    assert "zmux@alpine" in profile.read_text(encoding="utf-8")
    assert env["PS1"] == "zmux@alpine:\\w$ "


def test_build_command_line_requires_proot(monkeypatch):
    monkeypatch.delenv("ZMUX_PROOT_BIN", raising=False)
    monkeypatch.setattr(linuxenv, "_is_android", lambda: False)
    with pytest.raises(RuntimeError):
        linuxenv.build_command_line(["/bin/true"], linuxenv.HOME_DIR)


def test_build_command_line_uses_proot_binary(monkeypatch):
    monkeypatch.setenv("ZMUX_PROOT_BIN", "/bin/true")
    line = linuxenv.build_command_line(["/usr/bin/git", "clone", "https://x"],
                                       linuxenv.HOME_DIR)
    assert line.startswith("/bin/true -0 -r ")
    assert " -w /root " in line
    assert "/usr/bin/git clone https://x" in line
    assert ":/root" in line  # home bind present


def test_storage_target_is_bound_into_alpine(tmp_path, monkeypatch):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    external = tmp_path / "sdcard"
    external.mkdir()
    monkeypatch.setattr(linuxenv, "_ROOTFS_DIR", rootfs)
    monkeypatch.setenv("EXTERNAL_STORAGE", str(external))
    monkeypatch.delenv("ANDROID_APP_PATH", raising=False)
    monkeypatch.delenv("ANDROID_STORAGE", raising=False)
    monkeypatch.setattr(linuxenv, "_storage_bind_paths", lambda: [external])
    flags = linuxenv._bind_flags()
    assert f"{external}:{external}" in flags
    assert (rootfs / str(external).lstrip("/")).is_dir()


def test_safe_extract_rejects_traversal(tmp_path):
    import io
    evil = tmp_path / "evil.tar.gz"
    target = tmp_path / "out"
    with tarfile.open(evil, "w:gz") as tf:
        member = tarfile.TarInfo("../../escape.txt")
        member.size = 4
        tf.addfile(member, io.BytesIO(b"boom"))
    with pytest.raises(RuntimeError, match="unsafe"):
        linuxenv._safe_extract(evil, target)


def test_bootstrap_writes_repositories_and_resolv(tmp_path, monkeypatch):
    root = tmp_path / "rootfs"
    (root / "etc").mkdir(parents=True)
    monkeypatch.setattr(linuxenv, "_is_android", lambda: False)
    linuxenv._bootstrap(root)
    repos = (root / "etc" / "apk" / "repositories").read_text()
    assert "v3.22/main" in repos and "v3.22/community" in repos
    resolv = (root / "etc" / "resolv.conf").read_text()
    assert "nameserver" in resolv
    assert (root / "etc" / "alpine-release").read_text().strip() == linuxenv.ALPINE_VERSION


# ---------------------------------------------------------------------------
# Shell wiring (no proot needed for the honest-error paths)
# ---------------------------------------------------------------------------
def test_git_without_alpine_reports_honest_error(tmp_path, monkeypatch):
    monkeypatch.setattr(linuxenv, "is_installed", lambda: False)
    shell = PythonShell(tmp_path)
    result = shell.execute("git clone https://github.com/x/y")
    assert not result["ok"]
    assert result["exit_code"] == 1
    assert "linux-setup" in result["stderr"]


def test_linux_without_alpine_reports_honest_error(tmp_path, monkeypatch):
    monkeypatch.setattr(linuxenv, "is_installed", lambda: False)
    shell = PythonShell(tmp_path)
    result = shell.execute("linux apk add git")
    assert not result["ok"]
    assert "linux-setup" in result["stderr"]


def test_git_operator_guard_still_fires(tmp_path):
    shell = PythonShell(tmp_path)
    result = shell.execute("git clone x && cd y")
    assert result["exit_code"] == 2
    assert "'&&'" in result["stderr"]


def test_linux_help_without_args(tmp_path):
    shell = PythonShell(tmp_path)
    result = shell.execute("linux")
    assert result["ok"]
    assert "git" in result["stdout"]


# ---------------------------------------------------------------------------
# Integration: real proot + real Alpine rootfs (env-gated)
# ---------------------------------------------------------------------------
REAL_PROOT = os.environ.get("ZMUX_PROOT_BIN")
REAL_ROOTFS = os.environ.get("ZMUX_ROOTFS_DIR")

pytestmark = pytest.mark.skipif(
    not (REAL_PROOT and REAL_ROOTFS and Path(REAL_ROOTFS).is_dir()),
    reason="set ZMUX_PROOT_BIN and ZMUX_ROOTFS_DIR to run the real-proot tests",
)


def test_alpine_boots_inside_proot():
    shell = PythonShell(linuxenv.HOME_DIR)
    result = shell.execute("linux cat /etc/alpine-release")
    assert result["ok"], result["stderr"]
    assert result["stdout"].strip() == "3.22.5"


def test_apk_runs_inside_proot():
    shell = PythonShell(linuxenv.HOME_DIR)
    result = shell.execute("linux /sbin/apk --version")
    assert result["ok"], result["stderr"]
    assert "apk-tools" in result["stdout"]


def _host_git_binds() -> list:
    """Bind a dynamically-linked host git into the guest rootfs.

    The sandbox cannot `apk add git` (egress allowlist blocks Alpine's
    mirrors), so the end-to-end clone proof binds the host's git + its glibc
    instead. On a device with a full rootfs (`linux apk add git`) the plain
    `git` command is used instead — see the rootfs-git test below.
    """
    git = shutil.which("git")
    assert git, "host git required for the bind-git integration test"
    binds = [f"{git}:/usr/bin/git"]
    for directory in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
                      "/lib64", "/usr/share/git-core", "/usr/lib/git-core",
                      "/etc/ssl/certs"):  # host CA bundle for the host git
        if Path(directory).is_dir():
            binds.append(f"{directory}:{directory}")
    return binds


def test_real_git_clone_through_proot_binding_host_git():
    import shutil
    from zmux.paths import HOME_DIR
    shell = PythonShell(HOME_DIR)
    target = HOME_DIR / "zmux-itest-git"
    shutil.rmtree(target, ignore_errors=True)
    try:
        cmdline = linuxenv.build_command_line(
            ["/usr/bin/git", "clone", "--depth", "1",
             "https://github.com/muzape28-blip/ZABAWHEELS", target.name],
            HOME_DIR, extra_binds=_host_git_binds(),
        )
        env_extra = {**linuxenv.proot_env(),
                     "GIT_SSL_CAINFO": "/etc/ssl/certs/ca-certificates.crt"}
        result = shell._exec_subprocess(cmdline, None, env_extra=env_extra)
        assert result["ok"], result["stderr"]
        assert (target / "README.md").is_file()
        assert sum(1 for _ in target.rglob("*")) > 20
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _guest_has_git() -> bool:
    shell = PythonShell(linuxenv.HOME_DIR)
    result = shell.execute("linux sh -c 'command -v git || true'")
    return bool(result["ok"] and result["stdout"].strip())


def test_git_branch_inside_rootfs_when_git_installed():
    """Real `git` living inside Alpine: clone + branch listing.

    Only meaningful once the rootfs has git (`linux apk add git`). Skipped
    otherwise — the bind-git test above already proves the transport.
    """
    if not _guest_has_git():
        pytest.skip("guest rootfs has no git yet (run `linux apk add git`)")
    import shutil
    from zmux.paths import HOME_DIR
    shell = PythonShell(HOME_DIR)
    target = HOME_DIR / "zmux-itest-git"
    shutil.rmtree(target, ignore_errors=True)
    try:
        result = shell.execute(
            f"git clone --depth 1 https://github.com/muzape28-blip/ZABAWHEELS {target.name}"
        )
        assert result["ok"], result["stderr"]
        result = shell.execute(f"git -C {target.name} branch -a")
        assert result["ok"], result["stderr"]
        assert "main" in result["stdout"]
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_git_missing_inside_installed_alpine_hints_apk(monkeypatch):
    """Installed rootfs but no git yet -> say how to enable it, exit 1."""
    monkeypatch.setattr(linuxenv, "is_installed", lambda: True)
    monkeypatch.setattr(linuxenv, "rootfs_dir",
                        lambda: Path("/nonexistent-rootfs-no-git"))
    shell = PythonShell("/tmp")
    result = shell.execute("git clone https://github.com/x/y")
    assert not result["ok"]
    assert result["exit_code"] == 1
    assert "apk add git" in result["stderr"]


# ---------------------------------------------------------------------------
# talloc SONAME self-heal (the "libtalloc.so.2 not found" on-device failure)
# ---------------------------------------------------------------------------

def test_talloc_compat_mirrors_soname_when_missing(monkeypatch, tmp_path):
    """libtalloc.so exists but the linker wants libtalloc.so.2 -> mirror it."""
    lib_dir = tmp_path / "nativelibs"
    lib_dir.mkdir()
    (lib_dir / "libtalloc.so").write_bytes(b"ELF-talloc-content")
    runtime_dir = tmp_path / "runtime-lib"
    monkeypatch.setattr(linuxenv, "_RUNTIME_LIB_DIR", runtime_dir)
    result = linuxenv._ensure_talloc_compat(str(lib_dir))
    assert result == str(runtime_dir)
    assert (runtime_dir / "libtalloc.so.2").read_bytes() == b"ELF-talloc-content"


def test_talloc_compat_is_idempotent(monkeypatch, tmp_path):
    lib_dir = tmp_path / "nativelibs"
    lib_dir.mkdir()
    (lib_dir / "libtalloc.so").write_bytes(b"x")
    runtime_dir = tmp_path / "runtime-lib"
    monkeypatch.setattr(linuxenv, "_RUNTIME_LIB_DIR", runtime_dir)
    first = linuxenv._ensure_talloc_compat(str(lib_dir))
    second = linuxenv._ensure_talloc_compat(str(lib_dir))
    assert first == second == str(runtime_dir)
    assert len(list(runtime_dir.iterdir())) == 1


def test_talloc_compat_noop_when_source_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(linuxenv, "_RUNTIME_LIB_DIR", tmp_path / "runtime-lib")
    assert linuxenv._ensure_talloc_compat(str(tmp_path / "empty")) is None


def test_proot_env_android_prepends_talloc_compat_dir(monkeypatch, tmp_path):
    """proot_env on Android must point the linker at the mirrored SONAME file."""
    lib_dir = tmp_path / "nativelibs"
    lib_dir.mkdir()
    (lib_dir / "libtalloc.so").write_bytes(b"ELF")
    (lib_dir / "libproot-loader.so").write_bytes(b"ELF")
    runtime_dir = tmp_path / "runtime-lib"
    monkeypatch.setattr(linuxenv, "_RUNTIME_LIB_DIR", runtime_dir)
    monkeypatch.setattr(linuxenv, "native_library_dir", lambda: str(lib_dir))
    monkeypatch.setattr(linuxenv, "_is_android", lambda: True)
    monkeypatch.setattr(linuxenv, "CACHE_DIR", tmp_path / "cache")
    env = linuxenv.proot_env()
    parts = env["LD_LIBRARY_PATH"].split(os.pathsep)
    assert parts[0] == str(runtime_dir)          # mirrored SONAME wins first
    assert str(lib_dir) in parts                 # native dir still searched
    assert env["PROOT_LOADER"] == str(lib_dir / "libproot-loader.so")


def test_proot_env_android_skips_compat_when_dir_unknown(monkeypatch, tmp_path):
    """No nativeLibraryDir discovery -> no LD_LIBRARY_PATH games at all."""
    monkeypatch.setattr(linuxenv, "native_library_dir", lambda: None)
    monkeypatch.setattr(linuxenv, "_is_android", lambda: True)
    env = linuxenv.proot_env()
    assert "LD_LIBRARY_PATH" not in env


def test_talloc_compat_mirrors_plain_name_from_soname_file(monkeypatch, tmp_path):
    """APK ships libtalloc.so.2 only -> mirror to plain libtalloc.so too."""
    lib_dir = tmp_path / "nativelibs"
    lib_dir.mkdir()
    (lib_dir / "libtalloc.so.2").write_bytes(b"ELF-v2-content")
    runtime_dir = tmp_path / "runtime-lib"
    monkeypatch.setattr(linuxenv, "_RUNTIME_LIB_DIR", runtime_dir)
    result = linuxenv._ensure_talloc_compat(str(lib_dir))
    assert result == str(runtime_dir)
    assert (runtime_dir / "libtalloc.so").read_bytes() == b"ELF-v2-content"
    assert (runtime_dir / "libtalloc.so.2").read_bytes() == b"ELF-v2-content"


def test_talloc_compat_bidirectional_when_both_shipped(monkeypatch, tmp_path):
    """Both names present in nativeLibraryDir -> both mirrored (no-op repeat)."""
    lib_dir = tmp_path / "nativelibs"
    lib_dir.mkdir()
    (lib_dir / "libtalloc.so").write_bytes(b"A")
    (lib_dir / "libtalloc.so.2").write_bytes(b"B")
    runtime_dir = tmp_path / "runtime-lib"
    monkeypatch.setattr(linuxenv, "_RUNTIME_LIB_DIR", runtime_dir)
    assert linuxenv._ensure_talloc_compat(str(lib_dir)) == str(runtime_dir)
    assert (runtime_dir / "libtalloc.so").read_bytes() == b"B"  # v2 wins for the plain name
    assert (runtime_dir / "libtalloc.so.2").read_bytes() == b"A"
