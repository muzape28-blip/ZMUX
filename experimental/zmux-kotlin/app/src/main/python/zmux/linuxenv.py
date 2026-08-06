"""ZMUX Linux environment — a PRoot-based userspace Linux sandbox.

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
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from zmux.net import get_ssl_context
from zmux.paths import APP_DIR, CACHE_DIR, HOME_DIR, legacy_app_dir_candidates

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

ROOTFS_OS_MARKER = ".zmux-rootfs"
# ZMUX ships one supported guest: Alpine Linux. Rootfs detection requires
# the marker below to say "alpine"; unsupported/unrecognized rootfses are
# ignored and never reopened.
SUPPORTED_ROOTFS = frozenset(("alpine",))

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

# The Kotlin host knows ApplicationInfo.nativeLibraryDir exactly. Chaquopy
# isn't python-for-android, so the old Kivy/pyjnius discovery paths are often
# unavailable; this value is supplied explicitly before setup/proot probing.
_NATIVE_LIBRARY_DIR_OVERRIDE: str | None = os.environ.get("ZMUX_NATIVE_LIBRARY_DIR") or None


def set_native_library_dir(path: str | None) -> bool:
    """Set the trusted native library directory supplied by the Android host.

    Do not require an eager ``stat`` here: a Chaquopy worker can receive this
    value before Android exposes the directory to Python's filesystem layer.
    ``proot_binary`` performs the real file check afterwards and reports the
    exact expected path if packaging is broken.
    """
    global _NATIVE_LIBRARY_DIR_OVERRIDE
    try:
        candidate = Path(str(path or ""))
        if candidate.is_absolute():
            _NATIVE_LIBRARY_DIR_OVERRIDE = str(candidate)
            return True
    except (OSError, ValueError):
        pass
    return False


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------
def _is_android() -> bool:
    return bool(_NATIVE_LIBRARY_DIR_OVERRIDE) or any(
        k in os.environ for k in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH")
    )


#: Preferred device ABI as reported by Android's Build.SUPPORTED_ABIS.
#: Set by the Kotlin host (ZmuxTerminalActivity) *before* Python is started.
#: When present this overrides any heuristic based on os.uname(), because a
#: 32-bit Chaquopy runtime on a 64-bit ARM kernel reports ``armv8l`` — which
#: is *64-bit capable* — and the old code wrongly picked the 32-bit
#: ``armv7`` rootfs. Running armv7 binaries under proot on an aarch64
#: kernel triggers the untranslated-syscall hang that makes ``apk`` freeze.
_DEVICE_ABI_OVERRIDE = os.environ.get("ZMUX_DEVICE_ABI", "").strip()


def _android_abi_to_alpine(abi: str) -> str | None:
    abi = (abi or "").lower()
    if abi in ("arm64-v8a", "aarch64"):
        return "aarch64"
    if abi in ("armeabi-v7a", "armeabi", "armv7l", "armv7", "armv8l"):
        # armv8l is a 32-bit userspace on a 64-bit CPU; only return armv7
        # when the device has NO 64-bit ABI (caller checks SUPPORTED_ABIS).
        return "armv7"
    if abi in ("x86_64", "amd64"):
        return "x86_64"
    if abi in ("x86", "i686"):
        return "x86"
    return None


def alpine_arch() -> str:
    """Map the running platform to an Alpine minirootfs arch.

    Order of precedence:

    1. ``ZMUX_DEVICE_ABI`` env var (set by Kotlin from Build.SUPPORTED_ABIS).
    2. ``os.uname().machine`` heuristics for non-Android hosts (desktop tests).
    """
    if _is_android():
        # 1. Trust the host's explicit ABI override.
        if _DEVICE_ABI_OVERRIDE:
            mapped = _android_abi_to_alpine(_DEVICE_ABI_OVERRIDE)
            if mapped:
                return mapped
        # 2. Fall back to the kernel machine, but treat armv8l as aarch64:
        #    on Android that string means "32-bit process on a 64-bit CPU",
        #    and we want the 64-bit rootfs so proot does not have to run
        #    32-bit compat syscalls it does not translate.
        machine = os.uname().machine.lower()
        if machine in ("aarch64", "arm64", "arm64-v8a", "armv8l"):
            return "aarch64"
        if machine in ("armv7l", "armeabi-v7a", "arm"):
            return "armv7"
        if machine in ("x86_64", "amd64"):
            return "x86_64"
        if machine in ("x86", "i686"):
            return "x86"
        return machine
    # Non-Android (desktop / CI).
    machine = os.uname().machine.lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64", "armv8l"):
        return "aarch64"
    if machine in ("armv7l",):
        return "armv7"
    return machine


def rootfs_dir() -> Path:
    """Where the guest rootfs lives — the single source of truth.

    The Android host must *ask* for this path (Kotlin:
    ``linuxenv.callAttr("rootfs_dir")``) instead of rebuilding
    ``filesDir/linux/rootfs`` by hand. A second, hand-written copy of the
    layout is what produced "Rootfs path disappeared before PRoot launch":
    Python installed into Chaquopy's asset tree while Kotlin looked under
    ``filesDir``.
    """
    return _ROOTFS_DIR


def home_dir() -> Path:
    """Host directory bound to the guest's ``/root`` (see :data:`HOME_BIND`).

    Exposed for the same reason as :func:`rootfs_dir`: one owner for the
    layout, asked over the bridge rather than duplicated in Kotlin.
    """
    return HOME_DIR


def _normalise_os_name(os_name: str) -> str:
    """Validate the guest name. Alpine is the only supported guest."""
    value = str(os_name or "").strip().lower()
    if value not in SUPPORTED_ROOTFS:
        raise ValueError(
            f"unsupported Linux environment {os_name!r}; only 'alpine' is supported"
        )
    return value


def _guest_regular_file(root: Path, guest_path: str) -> bool:
    """Check a guest path without resolving absolute links against Android.

    Alpine's busybox applets are absolute guest links, e.g. ``/bin/sh ->
    /bin/busybox``. ``Path.is_file()`` follows that link in the *host*
    namespace, where Android has no ``/bin/busybox``; this made a complete
    Alpine extraction look broken. Resolve a small link chain inside ``root``
    instead, while refusing any relative target which would escape it.
    """
    root_abs = Path(os.path.abspath(root))
    candidate = root_abs / guest_path.lstrip("/")
    for _ in range(16):
        try:
            mode = candidate.lstat().st_mode
        except OSError:
            return False
        if stat.S_ISREG(mode):
            return True
        if not stat.S_ISLNK(mode):
            return False
        try:
            link_target = os.readlink(candidate)
        except OSError:
            return False
        candidate = (
            root_abs / link_target.lstrip("/")
            if os.path.isabs(link_target)
            else candidate.parent / link_target
        )
        candidate = Path(os.path.abspath(candidate))
        try:
            candidate.relative_to(root_abs)
        except ValueError:
            return False
    return False


def installed_os() -> str:
    """Return ``"alpine"`` when a complete Alpine rootfs exists, else ``""``.

    The check is strict: ``/bin/sh`` must be a regular guest file (resolving
    Alpine's absolute busybox symlinks inside the rootfs, not against the
    Android host) and the rootfs marker must name the supported OS.
    """
    root = rootfs_dir()
    if not _guest_regular_file(root, "/bin/sh"):
        return ""
    try:
        marker = (root / "etc" / ROOTFS_OS_MARKER).read_text("utf-8").strip().lower()
        if marker in SUPPORTED_ROOTFS:
            return marker
    except OSError:
        pass
    # Compatibility with Alpine installs made before the marker existed.
    if (root / "etc" / "alpine-release").is_file():
        return "alpine"
    return ""


def installed_arch() -> str:
    """Return the ``/etc/apk/arch`` string of the installed rootfs, or ``""``."""
    if installed_os() != "alpine":
        return ""
    arch_file = rootfs_dir() / "etc" / "apk" / "arch"
    try:
        return arch_file.read_text("utf-8").strip()
    except OSError:
        return ""


def is_installed() -> bool:
    return bool(installed_os())


def installed_version() -> str:
    if installed_os() != "alpine":
        return ""
    try:
        return (rootfs_dir() / "etc" / "alpine-release").read_text("utf-8").strip()
    except OSError:
        return ""


def status() -> str:
    os_name = installed_os()
    if not os_name:
        return "Linux environment: NOT INSTALLED (run `linux-setup`)"
    proot = proot_binary()
    proot_state = "ok" if proot else "missing libproot.so in nativeLibraryDir"
    return (f"{os_name.capitalize()} {installed_version()} ({alpine_arch()}) @ {rootfs_dir()}\n"
            f"proot: {proot_state}")


# ---------------------------------------------------------------------------
# Migration: installs made by a build with a mis-resolved APP_DIR
# ---------------------------------------------------------------------------
#: Directories the runtime creates itself; safe to remove from a stale APP_DIR
#: once they are empty. Anything else in Chaquopy's asset tree belongs to
#: Chaquopy (extracted Python sources) and is never touched.
_LEGACY_PRUNABLE_DIRS = (
    "linux",
    "bin",
    "cache",
    "logs",
    "staging",
    "installed",
    "user_packages",
    "home",
)


def _merge_tree(source: Path, destination: Path) -> int:
    """Move entries from ``source`` into ``destination`` without overwriting.

    Only renames (``os.replace``) are used: both trees live on the same
    app-private filesystem, so every move is O(1). An entry that cannot be
    renamed is left in place rather than copied — this runs on the Android UI
    thread during startup and must never turn into a multi-hundred-megabyte
    copy. Existing destination entries always win; directories present on both
    sides are merged recursively (``projects/`` is created eagerly by
    ``zmux.paths``, so a plain top-level skip would strand the user's files).
    Returns the number of entries moved.
    """
    moved = 0
    try:
        entries = sorted(source.iterdir())
    except OSError:
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        target = destination / entry.name
        if entry.is_dir() and not entry.is_symlink() and target.is_dir():
            moved += _merge_tree(entry, target)
            continue
        if target.exists() or target.is_symlink():
            continue
        try:
            os.replace(entry, target)
        except OSError:
            continue
        moved += 1
    with contextlib.suppress(OSError):
        source.rmdir()  # only succeeds once the husk is empty
    return moved


def migrate_legacy_install() -> str:
    """Adopt a rootfs installed by a build with a mis-resolved APP_DIR.

    Builds before the APP_DIR alignment contract let ``resolve_app_dir()`` fall
    back to Chaquopy's asset tree, so ``linux-setup`` installed the guest into
    ``<filesDir>/chaquopy/AssetFinder/app/linux/rootfs`` while the Kotlin
    launcher looked under ``<filesDir>/linux/rootfs``. Both live on the same
    filesystem, so the install is *renamed* into place (instant) instead of
    re-downloaded, and stale home files are merged in without overwriting.

    Never raises and never copies: on any failure the user is told to run
    ``linux-setup`` again, which reinstalls safely and keeps the user home.

    Returns a human-readable note for the terminal, or ``""`` when there was
    nothing to migrate (the normal case on a healthy device).
    """
    notes: list[str] = []
    for legacy_app_dir in legacy_app_dir_candidates():
        legacy_rootfs = legacy_app_dir / "linux" / "rootfs"
        target = rootfs_dir()
        if legacy_rootfs == target or not legacy_rootfs.is_dir():
            continue
        usable = _guest_regular_file(legacy_rootfs, "/bin/sh")
        if is_installed():
            # A working install at the live path wins; say where the orphan is
            # instead of silently deleting hundreds of megabytes.
            if usable:
                notes.append(
                    f"An older Linux install is still stored at {legacy_rootfs}; "
                    "it is no longer used and can be deleted to free space."
                )
            continue
        if not usable:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # Only an empty/aborted husk can be here: is_installed() is False.
                shutil.rmtree(target, ignore_errors=True)
            os.replace(legacy_rootfs, target)
        except OSError as error:
            notes.append(
                f"Found an older Linux install at {legacy_rootfs} but could not move it to "
                f"{target} ({error}). Run linux-setup to reinstall — your home directory is kept."
            )
            continue
        moved_home = _merge_tree(legacy_app_dir / "home", HOME_DIR)
        for name in _LEGACY_PRUNABLE_DIRS:
            with contextlib.suppress(OSError):
                # Depth-first: `linux/.staging` first, then `linux` itself.
                for husk in sorted((legacy_app_dir / name).rglob("*"), reverse=True):
                    if husk.is_dir() and not husk.is_symlink():
                        with contextlib.suppress(OSError):
                            husk.rmdir()
                (legacy_app_dir / name).rmdir()
        detail = f" ({moved_home} home item(s) migrated)" if moved_home else ""
        notes.append(
            f"Recovered the previous Linux install from {legacy_rootfs} into {target}{detail}."
        )
    return "\n".join(notes)


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
    # Chaquopy is not python-for-android and may not define any of the
    # ANDROID_* environment variables used by _is_android(). Kotlin passes
    # this authoritative value explicitly, so it must win before that legacy
    # runtime heuristic.
    if _NATIVE_LIBRARY_DIR_OVERRIDE:
        return _NATIVE_LIBRARY_DIR_OVERRIDE
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
        # Some Android kernels (esp. Android 14/15) deliver a fatal SIGSYS
        # ("Bad system call") to proot's tracees when proot uses its seccomp
        # accelerator. Disabling it keeps guests working; proot falls back to
        # pure ptrace.
        "PROOT_NO_SECCOMP": "1",
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


def _nameserver_ips(path: Path = Path("/etc/resolv.conf")) -> list[str]:
    """Return usable nameservers from a host resolv.conf, excluding stubs.

    Android/ROM resolv.conf files frequently point at a loopback resolver
    (127.0.0.1 / ::1) that the PRoot guest cannot reach. Filter those out so
    they never get copied into the guest, where they cause
    "Temporary failure resolving".
    """
    servers: list[str] = []
    try:
        for line in path.read_text("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].lower() == "nameserver":
                ip = parts[1].strip("%").split("%", 1)[0]
                low = ip.lower()
                # IPv4 only. Some mobile carriers hand out IPv6 DNS with no
                # working route, which made apk hang on DNS.
                if low in ("localhost", "::1") or low.startswith("127.") or ":" in low:
                    continue
                servers.append(ip)
    except OSError:
        pass
    return servers


def _ensure_guest_resolv_conf(root: Path | None = None) -> None:
    """Write a usable /etc/resolv.conf into the guest rootfs.

    We deliberately do NOT bind-mount the host /etc/resolv.conf anymore: on
    Android it is often a loopback stub the guest cannot use, which produced
    "Temporary failure resolving". Use public IPv4 DNS so the Alpine guest
    does not hang on a dead IPv6 resolver (some Indonesian carriers hand out
    IPv6 addresses with no working route). Repairing on every launch also
    heals rootfses installed before this fix.
    """
    root = root or rootfs_dir()
    etc = root / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    servers: list[str] = []
    for candidate in _nameserver_ips() + ["8.8.8.8", "1.1.1.1"]:
        if candidate not in servers:
            servers.append(candidate)
    content = (
        "# Generated by ZMUX — do not rely on the Android host resolver.\n"
        + "".join(f"nameserver {s}\n" for s in servers)
        + "options timeout:2 attempts:2\n"
    )
    try:
        (etc / "resolv.conf").write_text(content, encoding="utf-8")
    except OSError:
        pass


def _write_guest_prompt(root: Path | None = None) -> None:
    """Install the branded PS1 that wins over Alpine's /etc/profile default.

    Alpine's busybox ash sources /etc/profile, which sets a hostname-based
    prompt (``localhost:~#``) and then sources every /etc/profile.d/*.sh.
    Dropping our own script there overrides it reliably regardless of HOME.
    Raw ANSI escapes are used (no bash-only ``\\[ \\]``) so they work under
    busybox ash. The prompt is ``ZMUX:<path>$`` with the brand in ember and
    the path in teal.
    """
    root = root or rootfs_dir()
    profile_d = root / "etc" / "profile.d"
    profile_d.mkdir(parents=True, exist_ok=True)
    esc = "\033"
    ps1 = (
        f"{esc}[1;38;5;202mZMUX{esc}[0m:"
        f"{esc}[38;5;80m\\w{esc}[0m\\$ "
    )
    try:
        (profile_d / "zmux-prompt.sh").write_text(
            "# Managed by ZMUX — branded prompt.\n"
            f"PS1='{ps1}'\n"
            "export PS1\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _write_guest_motd(root: Path | None = None) -> None:
    """Write a small, static welcome banner shown by Alpine's /etc/profile.

    Pure ASCII/ANSI, no external commands — safe on low-end devices and in
    CI. It is idempotent: re-running overwrites the same file.
    """
    root = root or rootfs_dir()
    profile_d = root / "etc" / "profile.d"
    profile_d.mkdir(parents=True, exist_ok=True)
    try:
        (profile_d / "zmux-motd.sh").write_text(
            "# Managed by ZMUX — first-interactive-shell banner.\n"
            "if [ \"$SHLVL\" = \"1\" ]; then\n"
            "  printf '\\033[1;38;5;202m'\n"
            "  cat <<'BANNER'\n"
            "  ____  __  __ _   _ __  __\n"
            " |_  / |  \\/  | | | |\\ \\/ /\n"
            "  / /  | |\\/| | |_| | >  < \n"
            " /___| |_|  |_|\\___/ /_/\\_\\\n"
            "BANNER\n"
            "  printf '\\033[0m\\033[38;5;80m  Alpine %s\\033[0m\\n' \"$(cat /etc/alpine-release 2>/dev/null)\"\n"
            "  printf '\\033[90m  type \\033[36mapk add <pkg>\\033[90m to install packages\\033[0m\\n'\n"
            "  printf '\\n'\n"
            "fi\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _bind_flags() -> list:
    """proot bind flags shared by every invocation."""
    flags = ["-b", "/dev", "-b", "/proc", "-b", "/sys"]
    flags += ["-b", f"{HOME_DIR}:{HOME_BIND}"]
    for path in _storage_bind_paths():
        _ensure_guest_mountpoint(path)
        flags += ["-b", f"{path}:{path}"]
    # Note: /etc/resolv.conf is intentionally NOT bound — see
    # _ensure_guest_resolv_conf for why (Android loopback stubs break DNS).
    # Make sure existing installs also get a healthy resolver config.
    _ensure_guest_resolv_conf()
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
    if is_installed():
        _write_guest_prompt()
        _write_guest_motd()
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
    """Create the persistent guest-facing workspace.

    ``HOME_DIR`` is bind-mounted as /root, so this directory survives rootfs
    repair/reinstall and APK upgrades. The branded prompt itself lives in
    /etc/profile.d (see _write_guest_prompt) so it wins over Alpine's default
    PS1 and does not require touching the user's own ~/.profile.
    """
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    (HOME_DIR / "projects").mkdir(parents=True, exist_ok=True)


def interactive_env() -> dict:
    """Environment for the interactive PTY shell.

    proot_env() gives PATH/HOME/LD_LIBRARY_PATH (the loader contract for
    Android); TERM + LANG make TUI programs render correctly. The branded
    prompt is set via /etc/profile.d, this PS1 is only a fallback for shells
    started without sourcing /etc/profile.
    """
    ensure_user_home_layout()
    if is_installed():
        _write_guest_prompt()
        _write_guest_motd()
    env = proot_env()
    env["TERM"] = "xterm-256color"
    env["LANG"] = "C.UTF-8"
    env["SHELL"] = "/bin/sh"
    env["USER"] = "zmux"
    env["LOGNAME"] = "zmux"
    esc = "\033"
    env["PS1"] = (
        f"{esc}[1;38;5;202mZMUX{esc}[0m:"
        f"{esc}[38;5;80m\\w{esc}[0m\\$ "
    )
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
# Installation (download -> verified digest -> safe extract -> bootstrap)
# ---------------------------------------------------------------------------
def _rootfs_spec(os_name: str = "alpine") -> dict[str, object]:
    """Return the immutable, verified source description for Alpine."""
    _normalise_os_name(os_name)
    arch = alpine_arch()
    expected = ALPINE_SHA512.get(arch)
    if not expected:
        raise RuntimeError(f"No pinned Alpine rootfs for architecture {arch!r}")
    return {
        "os_name": "alpine",
        "label": f"Alpine {ALPINE_VERSION}",
        "url": (f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/releases/{arch}/"
                f"alpine-minirootfs-{ALPINE_VERSION}-{arch}.tar.gz"),
        "checksum_name": "sha512",
        "checksum": expected,
        "extension": "tar.gz",
    }


def _download_url(os_name: str = "alpine") -> tuple[str, str]:
    """Compatibility accessor for callers which only need URL and digest."""
    spec = _rootfs_spec(os_name)
    return str(spec["url"]), str(spec["checksum"])


def _emit_progress(callback, text: str) -> None:
    """Send terminal progress to Python callbacks *and* Chaquopy Java objects.

    Chaquopy 15 exposes a Kotlin object as a Java object, not a Python-callable
    function. The old code unconditionally did ``progress(text)`` and failed
    immediately with ``TypeError: ... object is not callable``. The Android
    activity then asked Python for a traceback outside an exception context,
    which is why the device only displayed the useless ``NoneType: None``.

    A disconnected activity must not turn an otherwise valid rootfs install
    into a failed one, so errors while rendering progress are deliberately
    ignored. The installer itself still raises download/verification/extraction
    failures normally.
    """
    if callback is None:
        return
    payload = text.replace("\n", "\r\n")
    try:
        if callable(callback):
            callback(payload)
            return
        # ``invoke`` is the explicit Java interface used by the Android UI.
        # accept/onProgress make this helper convenient for other Java bridges.
        for name in ("invoke", "accept", "onProgress"):
            method = getattr(callback, name, None)
            if callable(method):
                method(payload)
                return
    except Exception:
        return


def install(progress=None, os_name="alpine") -> dict:
    """Download, verify and atomically install the selected Linux rootfs.

    A selection replaces a different already-installed guest only after the
    new filesystem has been fully downloaded, verified and staged. The shared
    user home remains outside the rootfs and is therefore preserved.
    """
    spec = _rootfs_spec(str(os_name))
    selected = str(spec["os_name"])

    def report(text: str) -> None:
        _emit_progress(progress if progress is not None else progress_sink, text)

    # A rootfs stranded by an older build (Chaquopy asset tree) is adopted
    # before anything is downloaded, so a re-run of `linux-setup` heals the
    # layout instead of fetching the same guest a second time.
    migration_note = migrate_legacy_install()
    if migration_note:
        report(migration_note + "\n")

    current = installed_os()
    if current == selected:
        # If the rootfs arch does not match the device's best ABI
        # (e.g. a 32-bit armv7 rootfs under a 64-bit kernel because an
        # older ZMUX picked it from `uname -m`), treat it as not installed
        # so the caller re-downloads the correct rootfs. Running the wrong
        # arch under proot makes apk/libcrypto hang on syscalls that proot
        # does not translate in compat mode.
        wanted_arch = alpine_arch()
        have_arch = installed_arch()
        if have_arch and have_arch != wanted_arch:
            report(
                f"Installed rootfs is {have_arch} but this device is "
                f"{wanted_arch}; replacing it with the correct rootfs…\n"
            )
            shutil.rmtree(rootfs_dir(), ignore_errors=True)
            current = ""
        else:
            return {
                "ok": True,
                "already": True,
                "os": current,
                "version": installed_version(),
                "path": str(rootfs_dir()),
            }

    if current:
        report(f"Replacing installed {current.capitalize()} environment safely…\n")

    arch = alpine_arch()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tarball = CACHE_DIR / f"{selected}-rootfs-{arch}.{spec['extension']}"
    report(f"Downloading {spec['label']} rootfs ({arch})…\n")

    import hashlib
    checksum_name = str(spec["checksum_name"])
    digest = hashlib.new(checksum_name)
    total = 0
    started = time.monotonic()
    last = 0.0
    request = urllib.request.Request(
        str(spec["url"]), headers={"User-Agent": f"ZMUX/{APP_VERSION}"}
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
                    if time.monotonic() - last >= 0.15:
                        report(f"  {total / 1048576:5.1f} MiB "
                               f"({total / max(time.monotonic() - started, 1e-6) / 1024:5.1f} KiB/s)\r")
                        last = time.monotonic()
    except Exception as error:
        tarball.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {error}") from error

    actual = digest.hexdigest()
    expected = str(spec["checksum"])
    if actual.lower() != expected.lower():
        tarball.unlink(missing_ok=True)
        raise RuntimeError(
            f"{checksum_name.upper()} mismatch for {selected} rootfs:\n"
            f"  expected {expected}\n  actual   {actual}"
        )
    report(f"  {checksum_name.upper()} checksum verified ✓\n")

    _ROOTFS_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = _STAGING_DIR
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        report("Extracting rootfs into app-private storage…\n")
        _safe_extract(tarball, staging)
        report("Configuring Linux filesystem…\n")
        _bootstrap(staging, selected)
        # Atomic swap: never leave a half-installed rootfs at the live path.
        if rootfs_dir().exists():
            shutil.rmtree(rootfs_dir(), ignore_errors=True)
        os.replace(staging, rootfs_dir())
    except Exception as error:
        raise RuntimeError(
            f"{selected.capitalize()} rootfs install failed during extraction/configuration: {error}"
        ) from error
    finally:
        tarball.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "already": False,
        "os": selected,
        "version": installed_version(),
        "path": str(rootfs_dir()),
    }


def _safe_extract(tarball: Path, target: Path) -> None:
    """Extract the verified Alpine gzip tarball with traversal checks.

    Alpine minirootfs uses ``.tar.gz`` and no top-level directory stripping.
    The signature intentionally takes no ``strip_components`` argument because
    Alpine minirootfs extracts directly into the rootfs and a second archive
    shape is not supported.
    """
    with tarfile.open(tarball, "r:gz") as archive:
        members = []
        total = 0
        for member in archive.getmembers():
            name = member.name
            parts = tuple(part for part in Path(name).parts if part not in ("", "."))

            # Alpine's official minirootfs has a harmless top-level ``..``
            # directory metadata entry. It isn't payload and extracting it
            # would target the staging directory's parent, so ignore exactly
            # this root metadata entry. Never allow ``..`` in a real path.
            if member.isdir() and parts in ((), ("..",)):
                continue
            if name.startswith("/") or not parts or ".." in parts:
                raise RuntimeError(f"unsafe archive member: {name!r}")

            # Rootfs archives contain /dev nodes (null, zero, tty, ...). An
            # Android app UID must never call mknod(), which would raise
            # ``PermissionError: [Errno 1] Operation not permitted`` during
            # extraction. PRoot binds the real host /dev for every guest
            # invocation, so device entries are not only uncreatable but
            # unnecessary. Keep the empty /dev directory as a mountpoint.
            if (len(parts) > 1 and parts[0] == "dev") or member.ischr() or member.isblk() or member.isfifo():
                continue

            if member.isfile():
                total += member.size
                if total > MAX_ROOTFS_BYTES:
                    raise RuntimeError("uncompressed rootfs exceeds safety limit")
            members.append(member)

        # Python 3.14 changed extractall's default filter to "data", which
        # rejects the absolute symlink targets used by normal Linux rootfses.
        # Names and archive digest were validated above, so fully_trusted is
        # appropriate here. Keep the TypeError fallback for Python 3.11.
        try:
            archive.extractall(target, members=members, filter="fully_trusted")
        except TypeError:
            archive.extractall(target, members=members)


def _bootstrap(root: Path, os_name: str = "alpine") -> None:
    """Write first-run Alpine configuration and a durable OS marker."""
    _normalise_os_name(os_name)
    if not _guest_regular_file(root, "/bin/sh"):
        raise RuntimeError("rootfs is missing a usable guest /bin/sh after extraction")

    etc = root / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    # /dev payload entries are intentionally omitted: creating device nodes is
    # forbidden to an Android app UID, and PRoot overlays the host /dev. Keep
    # the mountpoints themselves explicit.
    for mountpoint in ("dev", "proc", "sys"):
        (root / mountpoint).mkdir(parents=True, exist_ok=True)

    apk = etc / "apk"
    apk.mkdir(parents=True, exist_ok=True)
    (apk / "repositories").write_text(
        f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/main\n"
        f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/community\n",
        encoding="utf-8",
    )
    # The official minirootfs normally contains this marker, but retain a
    # fallback for interrupted/cross-version upgrades.
    release = etc / "alpine-release"
    if not release.is_file():
        release.write_text(f"{ALPINE_VERSION}\n", encoding="utf-8")

    # DNS: never copy the Android host resolv.conf (it is often a loopback
    # stub the guest can't reach). Build a clean one with public resolvers.
    _ensure_guest_resolv_conf(root)
    _write_guest_prompt(root)
    _write_guest_motd(root)
    (etc / ROOTFS_OS_MARKER).write_text("alpine\n", encoding="utf-8")


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

    # G3 — the selected Linux rootfs boots inside proot.
    guest_name = installed_os()
    if proot and guest_name:
        try:
            line = build_command_line(["/bin/sh", "-c",
                                       "echo zmux-linux-ok; cat /etc/alpine-release"], HOME_DIR)
            env = dict(os.environ)
            env.update(proot_env())
            result = subprocess.run(line, shell=True, capture_output=True,
                                    text=True, timeout=60, env=env)
            ok = result.returncode == 0 and "zmux-linux-ok" in (result.stdout or "")
            gate("linux-boot", ok,
                 f"Alpine {installed_version()} boots in proot "
                 f"(exit={result.returncode}, out={result.stdout.strip()!r})")
        except Exception as error:
            gate("linux-boot", False, f"boot failed: {error}")
    else:
        gate("linux-boot", False,
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

    # G5 — Alpine's package manager is present. Network update is
    # environment-dependent and intentionally not part of this binary-level gate.
    if proot and guest_name:
        package_tool = ["/sbin/apk", "--version"]
        gate_name = "apk"
        try:
            line = build_command_line(package_tool, HOME_DIR)
            env = dict(os.environ)
            env.update(proot_env())
            result = subprocess.run(line, shell=True, capture_output=True,
                                    text=True, timeout=60, env=env)
            gate(gate_name, result.returncode == 0,
                 (result.stdout or result.stderr).strip())
        except Exception as error:
            gate(gate_name, False, f"{gate_name} check failed: {error}")
    else:
        gate("package-manager", False, "skipped: proot/rootfs unavailable")

    report_line("")
    passed = sum(1 for r in results.values() if r["ok"])
    report_line(f"[INFO] gates passed: {passed}/{len(results)}")
    return results
