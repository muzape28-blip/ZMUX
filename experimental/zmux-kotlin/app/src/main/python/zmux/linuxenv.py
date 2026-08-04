"""ZMUX Alpine Linux environment — a proot-based userspace Linux sandbox.

Why this exists
---------------
ZMUX is a Python-first virtual terminal: no PTY, no shell language, no TUI
programs, and Android's W^X policy blocks ``execve()`` on anything in the app
home directory. The one place Android *does* allow execution is the APK's
``nativeLibraryDir`` (the read-only ``lib/<abi>/`` extraction). PRoot runs
there: it is a tiny userland binary that emulates ``chroot`` + ``execve`` via
``ptrace`` and ``mmap(PROT_EXEC)`` — which the W^X policy explicitly permits
(``dlopen()``-style loading is not blocked). Bundling ``libproot.so`` in the
APK therefore gives ZMUX a real Alpine Linux userland: real ``git``, ``apk``,
``sh``, without root and without touching the host system.

Provenance (all pinned, all verifiable):
- Alpine minirootfs ``3.22.5`` is downloaded from the official Alpine CDN and
  verified against the official ``sha512`` published in the
  ``alpinelinux/docker-alpine`` ``v3.22`` branch (docker-alpine ships the
  minirootfs plus ``ca-certificates``, so TLS works out of the box).
- PRoot ``4dba3af`` (termux/proot) and talloc ``2.4.2`` are cross-compiled by
  ``scripts/build_proot_android.py`` (NDK) and shipped as ``libproot.so`` /
  ``libtalloc.so`` / ``libproot-loader*.so`` in the APK.
- Alpine 3.23+ is deliberately NOT used: apk-tools 3 calls ``execveat()``,
  which proot cannot translate (Kai 9000 pins 3.22.5 for the same reason).

Licensing note: PRoot is GPL-2.0+ (STMicroelectronics), talloc is LGPL-3.0.
ZMUX is AGPL-3.0; exec'ing a separate GPL binary is distribution-compatible,
and the proot/talloc sources are rebuilt by our own CI from pinned commits.

Public surface used by the terminal:
    status()                       human-readable one-line state
    install(progress=None)         download + verify + extract + bootstrap
    build_command_line(argv, cwd)  proot command line for the pipeline executor
    proot_env()                    extra child env (loader, libs, guest PATH)
    run_gates(print=...)           strict on-device probe (the "gates")
"""
from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from zmux.net import get_ssl_context
from zmux.paths import APP_DIR, CACHE_DIR, HOME_DIR

#: App version marker for the User-Agent (mirrors zmux.zpip.APP_VERSION).
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Pinned Alpine release (see module docstring for why not 3.23+)
# ---------------------------------------------------------------------------
ALPINE_VERSION = "3.22.5"
ALPINE_BRANCH = "v3.22"
ALPINE_MIRROR = os.environ.get(
    "ZMUX_ALPINE_MIRROR", "https://dl-cdn.alpinelinux.org/alpine"
).rstrip("/")

# Official SHA-512 digests from alpinelinux/docker-alpine branch v3.22
# (checksums.sha512). The download is rejected on any mismatch.
ALPINE_SHA512 = {
    "aarch64": "40ee819e0bab9b92c44a1edd176a3ae1b5020078a50f158d66011ba7be3325653526"
               "dadb4bcb896f7c6544c7cbbfd6904f0287f93ae447b3754e59d7e5679b2e",
    "armv7": "7c27652b0d5c9cd028cc3c6bae05bee48d2be564ac41b4afc8cad221c167b33af8e46c"
             "da822ad0555eff01127dfe4f384f8267709466f6a748131a12664188e2",
    "x86_64": "daf0cedbcbe47f1108bea745a0722e42f0ec0f0c08dd1e8b255234a48f2e5d45d0c96"
              "b437bcf60be2ee7ced261870a736e4831bc55924f73b23756283ecfb29b",
}

#: Guest PATH handed to processes inside the sandbox. The child env is built
#: by zmux.env for Android binaries; inside proot it must be Alpine's PATH or
#: `git`/`apk`/`sh` will not resolve.
GUEST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GUEST_HOME = "/root"

#: Host path bound to /root inside the sandbox: the user's ZMUX home.
#: Everything the terminal can `cd` into lives under HOME_DIR, so the whole
#: user workspace is reachable as ~/ inside Alpine.
HOME_BIND = "/root"

MAX_ROOTFS_BYTES = 256 * 1024 * 1024

#: Optional callback receiving progress text (already carriage-returned)
#: while the rootfs downloads. Set by the terminal so `linux-setup` streams
#: instead of blocking silently; left None for CLI callers, who get their
#: own progress lambda.
progress_sink = None

#: Overridable so tests (and power users) can point at an existing rootfs.
_ROOTFS_DIR = Path(os.environ.get("ZMUX_ROOTFS_DIR", APP_DIR / "linux" / "rootfs"))
_STAGING_DIR = APP_DIR / "linux" / ".staging"

#: Writable directory where proot's runtime dependencies can be mirrored
#: under their SONAME filenames. `nativeLibraryDir` is read-only, and some
#: APK packaging pipelines drop files that do not end in `.so` (like
#: `libtalloc.so.2`), so proot's DT_NEEDED cannot always be satisfied in
#: place. The self-heal in :func:`proot_env` copies the library here under
#: the exact name the linker asks for.
_RUNTIME_LIB_DIR = Path(os.environ.get("ZMUX_RUNTIME_LIB_DIR", APP_DIR / "lib"))


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------
def _is_android() -> bool:
    return any(k in os.environ for k in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH"))


def alpine_arch() -> str:
    """Map the running platform to an Alpine minirootfs arch."""
    if _is_android():
        machine = os.uname().machine.lower()
        bits = 32 if sys.maxsize <= 2**32 else 64
        if machine in ("aarch64", "arm64", "arm64-v8a"):
            return "aarch64"
        if machine in ("armv7l", "armv8l", "armeabi-v7a", "arm"):
            return "armv7"
        if machine in ("x86_64", "amd64"):
            return "x86_64"
        return machine
    machine = os.uname().machine.lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    if machine in ("armv7l", "armv8l"):
        return "armv7"
    return machine


def rootfs_dir() -> Path:
    return _ROOTFS_DIR


def is_installed() -> bool:
    return (rootfs_dir() / "bin" / "busybox").is_file() and (
        rootfs_dir() / "etc" / "alpine-release"
    ).is_file()


def installed_version() -> str:
    try:
        return (rootfs_dir() / "etc" / "alpine-release").read_text("utf-8").strip()
    except OSError:
        return ""


def status() -> str:
    if not is_installed():
        return "Alpine Linux environment: NOT INSTALLED (run `linux-setup`)"
    proot = proot_binary()
    proot_state = "ok" if proot else "missing libproot.so in nativeLibraryDir"
    return (f"Alpine {installed_version()} ({alpine_arch()}) @ {rootfs_dir()}\n"
            f"proot: {proot_state}")


# ---------------------------------------------------------------------------
# PRoot binary + child environment
# ---------------------------------------------------------------------------
def native_library_dir() -> str | None:
    """Android's nativeLibraryDir (where exec is allowed), or None on desktop.

    Resolution order:
    1. The primed Java bridge (``zmux.javabridge``) — the class is resolved
       once on the main thread at startup, so this works from worker threads
       where a raw pyjnius ``autoclass`` would raise ClassNotFoundException.
    2. A direct pyjnius lookup (works when the caller is the main thread).
    3. Scanning /proc/self/maps for an extracted libpython mapping (works
       even when the Java bridge is entirely unavailable).
    """
    if not _is_android():
        return None
    try:
        from zmux import javabridge
        activity = javabridge.mActivity()
        if activity is not None:
            value = activity.getApplicationInfo().nativeLibraryDir
            if value:
                return str(value)
    except Exception:
        pass
    try:
        from jnius import autoclass  # type: ignore
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        value = activity.getApplicationInfo().nativeLibraryDir
        if value:
            return str(value)
    except Exception:
        pass
    try:
        with open("/proc/self/maps", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "libpython" in line and "/lib/" in line:
                    path = line.split()[-1]
                    return str(Path(path).parent)
    except OSError:
        pass
    return None


def proot_binary() -> str | None:
    """Absolute path to an executable proot binary, or None."""
    if _is_android():
        lib_dir = native_library_dir()
        if lib_dir:
            candidate = os.path.join(lib_dir, "libproot.so")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None
    env_bin = os.environ.get("ZMUX_PROOT_BIN", "")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    return shutil.which("proot")


def _ensure_talloc_compat(lib_dir: str) -> str | None:
    """Mirror ``libtalloc`` under the filename the linker asks for.

    ``libproot.so`` records ``DT_NEEDED libtalloc.so.2`` (talloc's SONAME) in
    older APKs, and plain ``libtalloc.so`` in builds patched by the build
    script — and Android's linker matches ``DT_NEEDED`` against *exact*
    filenames. If the packaged file is only present under the other name,
    ``libproot.so`` refuses to start with

        CANNOT LINK EXECUTABLE ...: library "libtalloc.so.2" not found

    Because ``nativeLibraryDir`` is read-only, the compat copy is written to
    a writable runtime directory prepended to ``LD_LIBRARY_PATH``. Loading a
    native library from app-private storage is legal on Android (the same
    ``mmap(PROT_EXEC)`` path Termux uses for every binary in its
    ``$PREFIX/lib``), so this heals already-installed APKs without a rebuild.
    Returns the directory to add to ``LD_LIBRARY_PATH``, or None.
    """
    try:
        source = Path(lib_dir) / "libtalloc.so"
        source_v2 = Path(lib_dir) / "libtalloc.so.2"
        wanted_v2 = _RUNTIME_LIB_DIR / "libtalloc.so.2"
        wanted_plain = _RUNTIME_LIB_DIR / "libtalloc.so"
        if (source.is_file() or source_v2.is_file()) and (
            not wanted_v2.is_file() or not wanted_plain.is_file()
        ):
            _RUNTIME_LIB_DIR.mkdir(parents=True, exist_ok=True)
            # Chmod 700: the copy lives in app-private storage, keep it tight.
            try:
                os.chmod(_RUNTIME_LIB_DIR, 0o700)
            except OSError:
                pass
            if source.is_file() and not wanted_v2.is_file():
                shutil.copy2(source, wanted_v2)
            if source_v2.is_file() and not wanted_plain.is_file():
                shutil.copy2(source_v2, wanted_plain)
        if wanted_v2.is_file() or wanted_plain.is_file():
            return str(_RUNTIME_LIB_DIR)
    except OSError:
        pass
    return None


def proot_env() -> dict:
    """Extra environment variables every proot child needs."""
    extra: dict = {
        "PATH": GUEST_PATH,
        "HOME": GUEST_HOME,
    }
    if _is_android():
        lib_dir = native_library_dir() or ""
        if lib_dir:
            extra["LD_LIBRARY_PATH"] = lib_dir
            extra["PROOT_LOADER"] = os.path.join(lib_dir, "libproot-loader.so")
            extra["PROOT_TMP_DIR"] = str(CACHE_DIR)
            # Self-heal: old APKs package libtalloc.so under a name the
            # linker will not look for (see _ensure_talloc_compat). Prepend
            # the compat directory so the mirrored SONAME file wins.
            compat_dir = _ensure_talloc_compat(lib_dir)
            if compat_dir:
                extra["LD_LIBRARY_PATH"] = os.pathsep.join(
                    [compat_dir, extra["LD_LIBRARY_PATH"]]
                )
        # With no lib_dir at all there is nothing to mirror from and proot
        # cannot be located either; leave LD_LIBRARY_PATH unset and let the
        # proot_binary() lookup in build_command_line report the real error.
    else:
        # Host build of proot links a dynamic libtalloc; the builder writes
        # it next to the binary, so make the loader find it.
        binary = proot_binary()
        if binary:
            neighbor = os.path.dirname(os.path.realpath(binary))
            extra["LD_LIBRARY_PATH"] = neighbor
    return extra


def guest_cwd(host_cwd: Path) -> str:
    """Map a host path (always under HOME_DIR) to its guest path."""
    try:
        rel = Path(host_cwd).resolve().relative_to(HOME_DIR.resolve())
    except ValueError:
        return "/"
    if str(rel) == ".":
        return GUEST_HOME
    return f"{GUEST_HOME}/{rel.as_posix()}"


def _storage_bind_paths() -> list[Path]:
    """Host storage paths that must remain valid *inside* the PRoot guest.

    ``zmux-setup-storage`` creates links such as ``~/storage/downloads`` ->
    ``/sdcard/Download``. A symlink is visible through the /root bind, but its
    absolute target was previously not mounted in Alpine, producing the
    confusing ``ls`` works / ``cd`` says no such file error. Bind the target
    paths at their original guest paths as well.
    """
    candidates = [
        os.environ.get("ANDROID_APP_PATH", ""),
        os.environ.get("EXTERNAL_STORAGE", ""),
        os.environ.get("ANDROID_STORAGE", ""),
        "/sdcard",
        "/storage/emulated/0",
    ]
    paths: list[Path] = []
    for value in candidates:
        if not value:
            continue
        path = Path(value)
        if path.is_dir() and path not in paths:
            paths.append(path)
    return paths


def _ensure_guest_mountpoint(path: Path) -> None:
    """Create the rootfs-side destination used by an absolute storage bind."""
    try:
        # Only absolute host paths become guest absolute paths; strip the
        # leading slash so a malicious env value cannot escape the rootfs.
        relative = Path(str(path)).relative_to("/")
        (rootfs_dir() / relative).mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        pass


def _bind_flags() -> list:
    """proot bind flags shared by every invocation."""
    flags = ["-b", "/dev", "-b", "/proc", "-b", "/sys"]
    flags += ["-b", f"{HOME_DIR}:{HOME_BIND}"]
    for path in _storage_bind_paths():
        _ensure_guest_mountpoint(path)
        flags += ["-b", f"{path}:{path}"]
    resolv = Path("/etc/resolv.conf")
    if resolv.is_file():
        flags += ["-b", f"{resolv}:/etc/resolv.conf"]
    return flags


def build_proot_argv(guest_argv: list, host_cwd: Path,
                     extra_binds: list | None = None) -> list:
    """Return the full proot argv (list) for ``guest_argv``.

    ``extra_binds`` are ``src:dst`` pairs appended to the standard binds
    (used by the integration tests to bind a host git binary into a rootfs
    that has not had git installed yet).
    """
    proot = proot_binary()
    if not proot:
        raise RuntimeError(
            "proot is not available in this runtime (libproot.so missing)"
        )
    argv = [proot, "-0", "-r", str(rootfs_dir()), *_bind_flags()]
    for bind in extra_binds or ():
        argv += ["-b", bind]
    argv += ["-w", guest_cwd(host_cwd), *guest_argv]
    return argv


def build_command_line(guest_argv: list, host_cwd: Path,
                       extra_binds: list | None = None) -> str:
    """Return the full proot command line for the pipeline executor.

    ``python_shell`` feeds this string to its subprocess executor, so the
    result gets live streaming, Ctrl+C (killpg), timeouts and exit codes for
    free.
    """
    return shlex.join(build_proot_argv(guest_argv, host_cwd, extra_binds))


def build_interactive_argv(host_cwd: Path) -> list:
    """Proot argv for an *interactive login shell* (the PTY shell).

    Mirrors the battle-tested ``proot-distro login`` shape: ``--kill-on-exit``
    so killing proot tears down every tracee, ``--link2symlink`` and
    ``--sysvipc`` for normal application behaviour, then ``/bin/sh -l`` so
    Alpine's ``/etc/profile`` is sourced (login-shell semantics the old
    virtual terminal explicitly did not have).
    """
    argv = build_proot_argv(["/bin/sh", "-l"], host_cwd)
    argv.insert(1, "--kill-on-exit")
    argv.insert(2, "--link2symlink")
    argv.insert(3, "--sysvipc")
    return argv


def ensure_user_home_layout() -> None:
    """Create the persistent Alpine-facing workspace without touching files
    a user has already customised.

    ``HOME_DIR`` is bind-mounted as /root, so these files survive rootfs
    repair/reinstall and APK upgrades. A profile is created only on first use;
    existing user profiles always remain authoritative.
    """
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    (HOME_DIR / "projects").mkdir(parents=True, exist_ok=True)
    profile = HOME_DIR / ".profile"
    if not profile.exists():
        profile.write_text(
            "# Created by ZMUX. This file is yours to customise.\n"
            "export PS1='zmux@alpine:\\w$ '\n"
            "mkdir -p \"$HOME/projects\"\n",
            encoding="utf-8",
        )
    else:
        # Migrate only the exact profile line emitted by the previous ZMUX
        # build. A literal `$` is intentional: PRoot presents uid 0 inside
        # its guest, but ZMUX must not imply Android-root privilege with `#`.
        try:
            old = profile.read_text(encoding="utf-8")
            legacy = "export PS1='zmux@alpine:\\w\\$ '"
            if legacy in old:
                profile.write_text(old.replace(legacy, "export PS1='zmux@alpine:\\w$ '"), encoding="utf-8")
        except OSError:
            pass


def interactive_env() -> dict:
    """Environment for the interactive PTY shell.

    proot_env() gives PATH/HOME/LD_LIBRARY_PATH (the loader contract for
    Android); TERM + LANG make TUI programs (vim/htop/less) render correctly
    and set a friendly root prompt inside Alpine.
    """
    ensure_user_home_layout()
    env = proot_env()
    env["TERM"] = "xterm-256color"
    env["LANG"] = "C.UTF-8"
    env["SHELL"] = "/bin/sh"
    # Keep the product boundary visible without pretending this is Android
    # root. The guest is a normal Alpine userland rooted at its own /.
    env["USER"] = "zmux"
    env["LOGNAME"] = "zmux"
    env["PS1"] = "zmux@alpine:\\w$ "
    return env


def install_guest_wrappers() -> int:
    """Install small migration notices, never a route back to a host shell.

    Alpine is ZMUX's user-facing environment. Historic ZABAWHEELS commands
    must therefore not tell users to detach into an embedded Python console.
    ``zpip`` gets a useful Alpine package-management migration message; the
    remaining diagnostic names explicitly stay app-internal.
    """
    if not is_installed():
        return 0
    bin_dir = rootfs_dir() / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    notices = {
        "zpip": "#!/bin/sh\n"
                "echo 'zpip is retired in Alpine-first ZMUX.' >&2\n"
                "echo 'Use: apk add py3-<package>' >&2\n"
                "echo '  or: python3 -m pip install <package> (prefer a venv)' >&2\n"
                "exit 127\n",
        "gates": "#!/bin/sh\n"
                 "echo 'gates is an app diagnostic and is not part of Alpine.' >&2\n"
                 "exit 127\n",
        "zmux-info": "#!/bin/sh\n"
                     "echo 'zmux-info is available from ZMUX diagnostics.' >&2\n"
                     "exit 127\n",
        "zmux-pty-probe": "#!/bin/sh\n"
                          "echo 'zmux-pty-probe is an app diagnostic and is not part of Alpine.' >&2\n"
                          "exit 127\n",
        # Storage permission is an Android operation, not an apk command. The
        # OSC is consumed by PTYTerminalSession and forwarded to the trusted
        # app bridge without exposing the legacy host Python console.
        "zmux-setup-storage": "#!/bin/sh\n"
                              "printf '\\033]777;zmux-setup-storage\\007'\n"
                              "exit 0\n",
    }
    # Remove only stale wrappers generated by older ZMUX builds; never delete
    # a user-created command with the same name.
    for stale in ("linux-setup", "help"):
        candidate = bin_dir / stale
        try:
            if candidate.is_file() and "ZMUX host tool" in candidate.read_text(encoding="utf-8"):
                candidate.unlink()
        except OSError:
            pass

    written = 0
    for cmd, content in notices.items():
        wrapper = bin_dir / cmd
        try:
            current = wrapper.read_text(encoding="utf-8") if wrapper.exists() else None
            if current != content:
                wrapper.write_text(content, encoding="utf-8", newline="\n")
            os.chmod(wrapper, 0o755)
            written += 1
        except OSError:
            continue
    return written


# ---------------------------------------------------------------------------
# Installation (download -> sha512 verify -> safe extract -> bootstrap)
# ---------------------------------------------------------------------------
def _download_url() -> str:
    arch = alpine_arch()
    expected = ALPINE_SHA512.get(arch)
    if not expected:
        raise RuntimeError(f"No pinned Alpine rootfs for architecture {arch!r}")
    return (f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/releases/{arch}/"
            f"alpine-minirootfs-{ALPINE_VERSION}-{arch}.tar.gz"), expected


def install(progress=None) -> dict:
    """Download, verify and install the Alpine rootfs. Idempotent."""
    if is_installed():
        return {"ok": True, "already": True, "version": installed_version(),
                "path": str(rootfs_dir())}
    url, expected = _download_url()
    arch = alpine_arch()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tarball = CACHE_DIR / f"alpine-minirootfs-{ALPINE_VERSION}-{arch}.tar.gz"

    def _report(text: str) -> None:
        if progress is not None:
            progress(text.replace("\n", "\r\n"))
        elif progress_sink is not None:
            progress_sink(text.replace("\n", "\r\n"))

    _report(f"Downloading Alpine {ALPINE_VERSION} ({arch})…\n")
    digest, total = _hashlib_sha512(), 0
    started = time.monotonic()
    last = 0.0
    request = urllib.request.Request(
        url, headers={"User-Agent": f"ZMUX/{APP_VERSION}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120, context=get_ssl_context()) as response:
            announced = int(response.headers.get("Content-Length", "0") or 0)
            if announced > MAX_ROOTFS_BYTES:
                raise RuntimeError("rootfs tarball exceeds safety limit")
            with tarball.open("wb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ROOTFS_BYTES:
                        raise RuntimeError("rootfs tarball exceeds safety limit")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None and time.monotonic() - last >= 0.15:
                        _report(f"  {total / 1048576:5.1f} MiB "
                                f"({total / max(time.monotonic() - started, 1e-6) / 1024:5.1f} KiB/s)\r")
                        last = time.monotonic()
    except Exception as error:
        tarball.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {error}") from error

    actual = digest.hexdigest()
    if actual != expected:
        tarball.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-512 mismatch for Alpine rootfs:\n  expected {expected}\n  actual   {actual}"
        )
    _report("  checksum verified ✓\n")

    _ROOTFS_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = _STAGING_DIR
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _safe_extract(tarball, staging)
        _bootstrap(staging)
        # Atomic swap: never leave a half-installed rootfs at the live path.
        if rootfs_dir().exists():
            shutil.rmtree(rootfs_dir(), ignore_errors=True)
        os.replace(staging, rootfs_dir())
    finally:
        tarball.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "already": False, "version": installed_version(),
            "path": str(rootfs_dir())}


def _hashlib_sha512():
    import hashlib
    return hashlib.sha512()


def _safe_extract(tarball: Path, target: Path) -> None:
    """Extract the minirootfs with path-traversal protection."""
    with tarfile.open(tarball, "r:gz") as archive:
        members = []
        total = 0
        for member in archive.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError(f"unsafe archive member: {name!r}")
            if member.isfile():
                total += member.size
                if total > MAX_ROOTFS_BYTES:
                    raise RuntimeError("uncompressed rootfs exceeds safety limit")
            members.append(member)
        # Python 3.14 changed the extractall() default filter to "data",
        # which refuses absolute symlink targets — and a busybox-style
        # minirootfs is full of them (usr/bin/yes -> /bin/busybox, ~306
        # links), so `linux-setup` died on-device with "is a link to an
        # absolute path" while desktop CI (3.11, default None) passed.
        # "fully_trusted" is the honest filter here: member *names* are
        # already validated above (no absolute names, no "..", size cap)
        # and the archive itself is SHA-512-pinned to Alpine's official
        # digest, i.e. fully trusted content by construction.
        try:
            archive.extractall(target, members=members, filter="fully_trusted")
        except TypeError:
            # Python < 3.12 (and early patch levels) has no filter kwarg.
            archive.extractall(target, members=members)


def _bootstrap(root: Path) -> None:
    """First-run files every Alpine guest needs to be useful."""
    apk = root / "etc" / "apk"
    apk.mkdir(parents=True, exist_ok=True)
    (apk / "repositories").write_text(
        f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/main\n"
        f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/community\n",
        encoding="utf-8",
    )
    # DNS: prefer the host's resolv.conf; fall back to public resolvers.
    host_resolv = Path("/etc/resolv.conf")
    if host_resolv.is_file():
        try:
            content = host_resolv.read_text("utf-8", errors="replace")
            if content.strip():
                (root / "etc" / "resolv.conf").write_text(content, encoding="utf-8")
        except OSError:
            pass
    resolv = root / "etc" / "resolv.conf"
    if not resolv.is_file():
        resolv.write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n", encoding="utf-8")
    # Mark the version so `installed_version()` works even mid-upgrade.
    (root / "etc" / "alpine-release").write_text(
        f"{ALPINE_VERSION}\n", encoding="utf-8"
    ) if not (root / "etc" / "alpine-release").is_file() else None


def uninstall() -> dict:
    """Remove the installed rootfs (keeps cache)."""
    if rootfs_dir().exists():
        shutil.rmtree(rootfs_dir(), ignore_errors=True)
    return {"ok": True, "path": str(rootfs_dir())}


# ---------------------------------------------------------------------------
# Strict on-device probe ("zmux gates") — the acceptance test for this feature
# ---------------------------------------------------------------------------
def run_gates(report=None) -> dict:
    """Run every gate the real device must pass. Nothing here is mocked.

    Returns {name: {"ok": bool, "detail": str}}. ``report`` receives
    ``[PASS]/[FAIL]/[INFO]`` lines as they complete so the terminal streams
    progress. When no reporter is given, progress goes through
    :data:`progress_sink` if the terminal installed one, else ``print``.
    """
    if report is None:
        report = progress_sink if progress_sink is not None else print

    # ``print`` supplies a newline, but the live terminal progress sink writes
    # raw bytes directly to the PTY/WebSocket. Without this boundary, adjacent
    # gate results were glued together as `...possible[PASS]...` on phones.
    def report_line(text: str) -> None:
        report(text if text.endswith("\n") else text + "\n")

    results: dict = {}

    # Identify the exact build under test; "fixed APK still failing" reports
    # have repeatedly traced back to stale APKs, and the marker makes that
    # visible on the phone itself.
    try:
        from zmux.buildinfo import build_marker
        marker = build_marker()
        if marker:
            report_line(f"[INFO] zmux build: {marker}")
    except Exception:
        pass

    def gate(name, ok, detail):
        results[name] = {"ok": bool(ok), "detail": str(detail)}
        report_line(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        return bool(ok)

    # G1 — /dev/ptmx: can ZMUX ever be a real PTY terminal?
    try:
        fd = os.open("/dev/ptmx", os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
        gate("ptmx", True, "/dev/ptmx openable — PTY possible")
    except OSError as error:
        gate("ptmx", False, f"/dev/ptmx denied: {error}")

    # G2 — exec from nativeLibraryDir (the W^X gate).
    proot = proot_binary()
    if proot:
        # Read the actual DT_NEEDED from the shipped binary: a stale APK that
        # still asks for "libtalloc.so.2" is the classic "fixed build still
        # fails" trap, and a binary that fails to parse was corrupted by a
        # non length-preserving rewrite. Both are visible on the phone itself.
        stale_hint = ""
        try:
            from zmux import elfscan
            needed = elfscan.elf_dynamic_needed(proot)
            talloc_entry = next((n for n in needed if n.startswith("libtalloc")), None)
            if talloc_entry == "libtalloc.so.2":
                stale_hint = (
                    f" — STALE BINARY: this libproot.so still needs "
                    f"{talloc_entry!r}. Reinstall the build whose `zmux-info` "
                    "shows a Build: SHA"
                )
            elif talloc_entry is None:
                stale_hint = f" — NOTE: no libtalloc dependency found in {needed}"
        except Exception as scan_error:
            stale_hint = (
                " — UNREADABLE ELF: libproot.so does not parse "
                f"({type(scan_error).__name__}). This is a corrupted build — "
                "reinstall the latest APK"
            )
            needed = []
        try:
            env = dict(os.environ)
            env.update(proot_env())
            result = subprocess.run([proot, "--version"], capture_output=True,
                                    text=True, timeout=30, env=env)
            detail = (result.stdout or result.stderr).strip().splitlines()
            first = detail[0] if detail else "(no output)"
            gate("proot-exec", result.returncode == 0,
                 f"exec {proot} OK — {first}{stale_hint}")
        except Exception as error:
            gate("proot-exec", False, f"exec {proot} failed: {error}{stale_hint}")
    else:
        gate("proot-exec", False, "libproot.so not found/executable")

    # G3 — Alpine rootfs boots inside proot.
    if proot and is_installed():
        try:
            line = build_command_line(["/bin/sh", "-c",
                                       "echo zmux-alpine-ok; cat /etc/alpine-release"],
                                      HOME_DIR)
            env = dict(os.environ)
            env.update(proot_env())
            result = subprocess.run(line, shell=True, capture_output=True,
                                    text=True, timeout=60, env=env)
            ok = result.returncode == 0 and "zmux-alpine-ok" in (result.stdout or "")
            gate("alpine-boot", ok,
                 f"Alpine {installed_version()} boots in proot "
                 f"(exit={result.returncode}, out={result.stdout.strip()!r})")
        except Exception as error:
            gate("alpine-boot", False, f"boot failed: {error}")
    else:
        gate("alpine-boot", False,
             "rootfs not installed or proot missing (run `linux-setup` first)")

    # G4 — real `git clone` over HTTPS inside the sandbox. The target lives
    # under HOME_DIR (bound to /root inside the guest) so both the guest git
    # and the host check see the same directory.
    if proot and is_installed():
        target = HOME_DIR / ".zmux-gates-clone"
        shutil.rmtree(target, ignore_errors=True)
        try:
            guest_target = guest_cwd(target)
            line = build_command_line(["/usr/bin/git", "clone", "--depth", "1",
                                       "https://github.com/muzape28-blip/ZABAWHEELS",
                                       guest_target], HOME_DIR)
            env = dict(os.environ)
            env.update(proot_env())
            result = subprocess.run(line, shell=True, capture_output=True,
                                    text=True, timeout=300, env=env)
            files = sum(1 for _ in target.rglob("*")) if target.is_dir() else 0
            stderr_tail = (result.stderr or "").strip().splitlines()[-2:]
            detail = f"git clone over HTTPS: exit={result.returncode}, files={files}"
            if not (result.returncode == 0 and files > 0) and stderr_tail:
                detail += " — " + " / ".join(stderr_tail)
            gate("git-clone", result.returncode == 0 and files > 0, detail)
        except Exception as error:
            gate("git-clone", False, f"clone failed: {error}")
        finally:
            shutil.rmtree(target, ignore_errors=True)
    else:
        gate("git-clone", False, "skipped: proot/rootfs unavailable")

    # G5 — apk tooling present inside the guest (binary level; network update
    # is environment-dependent and reported as INFO, not a gate failure).
    if proot and is_installed():
        try:
            line = build_command_line(["/sbin/apk", "--version"], HOME_DIR)
            env = dict(os.environ)
            env.update(proot_env())
            result = subprocess.run(line, shell=True, capture_output=True,
                                    text=True, timeout=60, env=env)
            gate("apk", result.returncode == 0,
                 (result.stdout or result.stderr).strip())
        except Exception as error:
            gate("apk", False, f"apk check failed: {error}")
    else:
        gate("apk", False, "skipped: proot/rootfs unavailable")

    report_line("")
    passed = sum(1 for r in results.values() if r["ok"])
    report_line(f"[INFO] gates passed: {passed}/{len(results)}")
    return results
