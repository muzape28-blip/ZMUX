"""ZMUX runtime diagnostics shared by CLI, server, and legacy zpip.

This module deliberately contains no package-manager logic. Historically the
``zmux-info`` fingerprint lived in :mod:`zmux.zpip`, which made diagnostics
look coupled to the retiring ZABAWHEELS package ecosystem. Keeping the
fingerprint here lets app diagnostics survive after ``zpip`` is removed.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import struct
import sysconfig
from pathlib import Path

from zmux.buildinfo import build_marker
from zmux.paths import APP_DIR, legacy_package_paths

APP_VERSION = "1.0.0"
LEGACY_PACKAGE_PATHS = legacy_package_paths()
DB_FILE = LEGACY_PACKAGE_PATHS["installed"] / "packages.json"


def android_abi() -> str:
    """Return the Android/Python ABI name used by ZMUX package diagnostics."""
    machine = platform.machine().lower()
    is_32bit = struct.calcsize("P") * 8 == 32
    if is_32bit and machine in {"aarch64", "arm64", "arm64-v8a", "armv7l", "armv8l", "armeabi-v7a", "arm"}:
        return "armeabi-v7a"
    if machine in {"armv7l", "armv8l", "armeabi-v7a"}:
        return "armeabi-v7a"
    if machine in {"aarch64", "arm64", "arm64-v8a"}:
        return "arm64-v8a"
    return machine


def _load_installed_db() -> dict:
    """Read the legacy zpip installed-package database without importing zpip."""
    try:
        data = json.loads(DB_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def runtime_fingerprint() -> dict:
    """Build the runtime fingerprint for ``zmux-info`` and diagnostics.

    Values that can only be known at runtime are marked with observed data.
    Build-contract values mirror the current Android toolchain lock.
    """
    version = platform.python_version()
    short = "".join(version.split(".")[:2])
    p4a = "5c192d7b7308"
    ndk = "28c"

    fp = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        # On-device build identity: CI writes build_marker.txt (git SHA +
        # workflow run id) into the packaged app; '' on desktop/ad-hoc builds.
        "build": build_marker(),
        "runtime_id": f"zmux-py{short}-api26-p4a{p4a}-r1",
        "python": {
            "implementation": platform.python_implementation(),
            "version": version,
            "soabi": sysconfig.get_config_var("SOABI") or "",
            "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX") or "",
            "pointer_bits": struct.calcsize("P") * 8,
        },
        "android": {
            "abi": android_abi(),
            "api": int(os.environ.get("ANDROID_API", "0") or 0),
            "release": platform.release(),
        },
        "build_contract": {
            "p4a_commit": p4a,
            "ndk": ndk,
        },
        "paths": {
            "cwd": str(Path.cwd()),
            # Kept while the legacy zpip database still exists; this should be
            # renamed/removed when the package ecosystem is retired.
            "user_packages": str(LEGACY_PACKAGE_PATHS["user_packages"]),
            "home": str(Path.home()),
        },
        "storage": {
            "free_bytes": shutil.disk_usage(str(APP_DIR)).free,
        },
        "installed": list(_load_installed_db().keys()),
    }
    fp["legacy_package_db"] = {
        "status": "compatibility-only",
        "manager": "zpip",
        "user_packages": fp["paths"]["user_packages"],
        "installed_db": str(DB_FILE),
        "installed_count": len(fp["installed"]),
    }

    # On-device proof of which proot binary is actually shipped: read the
    # DT_NEEDED of libproot.so from nativeLibraryDir. A stale binary that still
    # asks for "libtalloc.so.2" is the classic "fixed build still fails" trap.
    proot = None
    try:
        from zmux import elfscan, linuxenv
        lib_dir = linuxenv.native_library_dir()
        proot = os.path.join(lib_dir, "libproot.so") if lib_dir else None
        if proot and os.path.isfile(proot):
            needed = elfscan.elf_dynamic_needed(proot)
            fp["proot"] = {
                "binary": proot,
                "needed": needed,
                "talloc": next((n for n in needed if n.startswith("libtalloc")), None),
            }
        elif proot:
            fp["proot"] = {
                "binary": proot,
                "error": "libproot.so is present but unreadable as ELF "
                         "(corrupted build — reinstall the latest APK)",
            }
    except Exception as error:
        fp["proot"] = {
            "binary": proot or "(unknown)",
            "error": f"could not read libproot.so ({type(error).__name__}: {error})",
        }
    return fp


def format_fingerprint(fp: dict) -> str:
    """Render the runtime fingerprint for the ``zmux-info`` command."""
    lines = [
        "ZMUX Runtime Fingerprint",
        "=" * 40,
        f"App version:        {fp['app_version']}",
        f"Build:              {fp.get('build') or '(not recorded)'}",
        *(
            []
            if "proot" not in fp
            else [f"Proot NEEDED:       {', '.join(fp['proot'].get('needed') or []) or '(none)'}"]
        ),
        *(
            []
            if "proot" not in fp
            else [f"Proot talloc:       {fp['proot'].get('talloc') or '(none)'}"
                  + ("" if fp["proot"].get("talloc") == "libtalloc.so"
                     else "  [STALE — reinstall the latest build]")]
        ),
        *(
            []
            if "proot" not in fp or not fp["proot"].get("error")
            else [f"Proot status:      {fp['proot']['error']}"]
        ),
        f"Python version:     {fp['python']['version']}",
        f"Implementation:     {fp['python']['implementation']}",
        f"SOABI:              {fp['python']['soabi']}",
        f"EXT_SUFFIX:         {fp['python']['ext_suffix']}",
        f"Pointer bits:       {fp['python']['pointer_bits']}",
        f"ABI:                {fp['android']['abi']}",
        f"Android API:        {fp['android']['api']}",
        f"Runtime ID:         {fp['runtime_id']}",
        f"p4a commit:         {fp['build_contract']['p4a_commit']}",
        f"NDK:                {fp['build_contract']['ndk']}",
        f"CWD:                {fp['paths']['cwd']}",
        f"Legacy user packages: {fp['paths']['user_packages']}",
        f"Legacy package DB:  {(fp.get('legacy_package_db') or {}).get('status', 'compatibility-only')}",
        f"Free storage:       {fp['storage']['free_bytes']:,} bytes",
    ]
    installed = fp["installed"]
    if installed:
        lines.append(f"Installed packages (legacy zpip): {', '.join(installed)}")
    else:
        lines.append("Installed packages (legacy zpip): (none)")
    return "\n".join(lines)
