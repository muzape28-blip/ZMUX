"""
ZMUX zpip — Transactional, hash-verifying package manager.

The installer accepts curated ZABAWHEELS manifests first and can optionally use
PyPI *universal* wheels. Native wheels are never guessed: their runtime id and
Android ABI must match the running application.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import sysconfig
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion, Version
except ImportError:
    # Minimal fallback when packaging is not available
    class InvalidRequirement(Exception):
        pass
    class InvalidVersion(Exception):
        pass
    class Requirement:
        def __init__(self, s):
            parts = re.split(r"[><=!~\s]", s, maxsplit=1)
            self.name = parts[0]
            self.specifier = None
    class Version:
        def __init__(self, s):
            self.parts = tuple(int(x) for x in str(s).split(".") if x.isdigit())

from zmux.net import get_ssl_context
from zmux.paths import (
    APP_DIR,
    CACHE_DIR,
    USER_PACKAGES_DIR,
    INSTALLED_DIR,
    DOWNLOADS_DIR,
    STAGING_DIR,
)
from zmux.buildinfo import build_marker

APP_VERSION = "1.0.0"
INDEX_URL = os.environ.get(
    "ZMUX_WHEEL_INDEX",
    "https://muzape28-blip.github.io/ZABAWHEELS/index/v1",
).rstrip("/")
MAX_WHEEL_BYTES = 100 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_QUERY_TOKEN = re.compile(r"[a-z0-9][a-z0-9+._-]*")

# Search-time network budget and curated catalog cache freshness. The catalog
# is small and changes rarely, so an hour of freshness avoids refetching on
# every keystroke-query while a stale copy remains usable offline.
SEARCH_TIMEOUT = 8
CATALOG_TTL_SECONDS = 3600

DB_FILE = INSTALLED_DIR / "packages.json"
LEGACY_PACKAGE_MANAGER = True
LEGACY_STATUS = "compatibility-only"
LEGACY_NOTICE = (
    "zpip is retained for compatibility only. ZMUX user package workflow is "
    "Alpine apk plus Python venv/pip."
)


def _mark_legacy_result(result: dict) -> dict:
    """Annotate a dispatch result without changing command-specific fields."""
    result.setdefault("legacy", True)
    result.setdefault("legacy_status", LEGACY_STATUS)
    result.setdefault("legacy_notice", LEGACY_NOTICE)
    result.setdefault("manager", "zpip")
    return result

#: PyPI distribution names that collide with famous command-line tools.
#: `zpip install nano` resolves to PyPI (no curated manifest), and PyPI's
#: ``nano`` is a Django mini-app library — not the GNU nano editor a user
#: typing `nano` almost certainly wants. The GNU editor is a C binary that
#: needs a real TTY anyway, which ZMUX's virtual terminal does not provide,
#: so the honest move is a loud warning instead of silent "success".
PYPI_TOOL_COLLISIONS = {
    "nano": (
        "PyPI's 'nano' is a Django mini-app library, NOT the GNU nano editor. "
        "GNU nano also needs a real TTY, which ZMUX does not provide — use the "
        "Python runtime to edit files (python / open(...).write(...))."
    ),
}


def _tool_collision_warning(package: str) -> str | None:
    """Warning text when a package name collides with a well-known tool."""
    return PYPI_TOOL_COLLISIONS.get(canonicalize(package))


def canonicalize(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name.strip()):
        raise ValueError("Invalid package name")
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _is_android() -> bool:
    return any(k in os.environ for k in ("ANDROID_PRIVATE", "ANDROID_ARGUMENT", "ANDROID_APP_PATH"))


def android_abi() -> str:
    machine = platform.machine().lower()
    is_32bit = struct.calcsize("P") * 8 == 32
    if is_32bit and machine in {"aarch64", "arm64", "arm64-v8a", "armv7l", "armv8l", "armeabi-v7a", "arm"}:
        return "armeabi-v7a"
    if machine in {"armv7l", "armv8l", "armeabi-v7a"}:
        return "armeabi-v7a"
    if machine in {"aarch64", "arm64", "arm64-v8a"}:
        return "arm64-v8a"
    return machine


def runtime_fingerprint() -> dict:
    """Build the runtime fingerprint.
    
    Values that can only be known at runtime are marked with 'observed'.
    Build-contract values come from toolchain/runtime-lock.json.
    """
    version = platform.python_version()
    short = "".join(version.split(".")[:2])
    p4a = "5c192d7b7308"
    ndk = "28c"
    
    fp = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        # On-device build identity: the CI writes build_marker.txt (git SHA +
        # workflow run id) into the packaged app; '' on desktop or ad-hoc
        # builds. Lets `zmux-info`/`gates` prove which build a phone runs.
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
            "user_packages": str(USER_PACKAGES_DIR),
            "home": str(Path.home()),
        },
        "storage": {
            "free_bytes": shutil.disk_usage(str(APP_DIR)).free,
        },
        "installed": list(_load_db().keys()),
    }
    # On-device proof of which proot binary is actually shipped: read the
    # DT_NEEDED of libproot.so from nativeLibraryDir. A stale binary that
    # still asks for "libtalloc.so.2" is the classic "fixed build still
    # fails" trap; a binary that fails to parse was corrupted by a non
    # length-preserving rewrite (see scripts/build_proot_android.py). Both
    # are reported explicitly instead of silently dropping the section.
    try:
        from zmux import elfscan, linuxenv
        lib_dir = linuxenv.native_library_dir()
        proot = os.path.join(lib_dir, "libproot.so") if lib_dir else None
        if proot and os.path.isfile(proot):
            needed = elfscan.elf_dynamic_needed(proot)
            fp["proot"] = {
                "binary": proot,
                "needed": needed,
                "talloc": next(
                    (n for n in needed if n.startswith("libtalloc")), None
                ),
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


def _load_db() -> dict:
    try:
        data = json.loads(DB_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_db(data: dict) -> None:
    INSTALLED_DIR.mkdir(parents=True, exist_ok=True)
    temp = DB_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, DB_FILE)


def _offline() -> bool:
    """Honest offline switch: ZMUX_OFFLINE=1 skips every network fetch.

    Package *installs* still require the network (a wheel must come from
    somewhere), but metadata commands such as ``zpip search`` degrade to the
    on-device cache instead of blocking on timeouts.
    """
    return os.environ.get("ZMUX_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def _request_json(url: str, timeout: int = 25) -> dict:
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS package metadata is accepted")
    req = urllib.request.Request(url, headers={"User-Agent": f"ZMUX/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as response:
        if int(response.headers.get("Content-Length", "0") or 0) > 5 * 1024 * 1024:
            raise ValueError("Package metadata is too large")
        value = json.loads(response.read(5 * 1024 * 1024 + 1))
    if not isinstance(value, dict):
        raise ValueError("Package metadata must be a JSON object")
    return value


#: Optional callback receiving progress text (already carriage-returned) while
#: a wheel downloads. Set by the terminal so installs are not a silent block;
#: left None for REST/CLI callers, where the download stays quiet.
progress_sink = None

#: Minimum interval between progress repaints, so a fast link does not spend
#: all its time writing to the websocket (rpkg throttles the same way).
_PROGRESS_INTERVAL = 0.1


def _render_progress(name: str, done: int, total: int, started: float) -> str:
    """One-line download progress bar: name, size, rate, bar, percent."""
    elapsed = max(time.monotonic() - started, 1e-6)
    rate = done / 1024 / elapsed
    label = name if len(name) <= 18 else name[:15] + "..."
    if total > 0:
        fraction = min(done / total, 1.0)
        filled = int(20 * fraction)
        bar = "#" * filled + "-" * (20 - filled)
        pct = f"{fraction * 100:3.0f}%"
    else:  # server sent no Content-Length
        bar = "-" * 20
        pct = "  ?%"
    return (
        f"\x1b[2K\r{label:<18} {done / 1048576:5.1f} MiB "
        f"{rate:6.1f} KiB/s [{bar}] {pct}"
    )


def _download(url: str, expected_hash: str, destination: Path, label: str = "") -> tuple:
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS wheel downloads are accepted")
    if not _SHA256.fullmatch(expected_hash):
        raise ValueError("A valid SHA-256 is required")
    request = urllib.request.Request(url, headers={"User-Agent": f"ZMUX/{APP_VERSION}"})
    digest, total = hashlib.sha256(), 0
    sink = progress_sink
    started = time.monotonic()
    last_paint = 0.0
    with urllib.request.urlopen(request, timeout=90, context=get_ssl_context()) as response:
        announced = int(response.headers.get("Content-Length", "0") or 0)
        if announced > MAX_WHEEL_BYTES:
            raise ValueError("Wheel exceeds the 100 MiB safety limit")
        with destination.open("wb") as output:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_WHEEL_BYTES:
                    raise ValueError("Wheel exceeds the 100 MiB safety limit")
                digest.update(chunk)
                output.write(chunk)
                if sink is not None:
                    now = time.monotonic()
                    if now - last_paint >= _PROGRESS_INTERVAL:
                        sink(_render_progress(label or "downloading", total, announced, started))
                        last_paint = now
    if sink is not None:
        # Final repaint at 100%, then end the line so the next output starts clean.
        sink(_render_progress(label or "downloading", total, total, started) + "\n")
    actual = digest.hexdigest()
    if actual != expected_hash:
        destination.unlink(missing_ok=True)
        raise ValueError(f"SHA-256 mismatch: expected {expected_hash}, got {actual}")
    return total, actual


def _safe_members(wheel: Path) -> list:
    seen = set()
    members = []
    total = 0
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "" in path.parts:
                raise ValueError(f"Unsafe archive path: {member.filename}")
            if member.filename in seen:
                raise ValueError(f"Duplicate archive member: {member.filename}")
            if member.is_dir():
                continue
            seen.add(member.filename)
            total += member.file_size
            if member.file_size > MAX_WHEEL_BYTES or total > MAX_WHEEL_BYTES * 3:
                raise ValueError("Uncompressed wheel content exceeds safety limit")
            members.append(member)
    if not any(item.filename.endswith(".dist-info/WHEEL") for item in members):
        raise ValueError("Wheel metadata is missing")
    if not any(item.filename.endswith(".dist-info/RECORD") for item in members):
        raise ValueError("Wheel RECORD is missing")
    return members


def _extract(wheel: Path, staging: Path) -> list:
    members = _safe_members(wheel)
    staging_root = staging.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for member in members:
            destination = (staging / member.filename).resolve()
            if destination != staging_root and staging_root not in destination.parents:
                raise ValueError("Wheel attempted to escape staging")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    return [item.filename for item in members]


def _manifest_from_curated(name: str, channel: str):
    fp = runtime_fingerprint()
    abi = fp["android"]["abi"]
    runtime_id = fp["runtime_id"]
    try:
        index = _request_json(f"{INDEX_URL}/runtimes/{runtime_id}/{abi}.json")
    except Exception:
        return None
    package = (index.get("packages") or {}).get(name)
    if not isinstance(package, dict) or package.get("channel") != channel:
        return None
    if package.get("runtime_id") != runtime_id or package.get("abi") != abi:
        raise ValueError("Curated wheel does not match this runtime and ABI")
    return package


def _manifest_from_pypi(name: str, version: str = None) -> dict:
    endpoint = f"https://pypi.org/pypi/{urllib.parse.quote(name)}"
    if version:
        endpoint += f"/{urllib.parse.quote(version)}"
    metadata = _request_json(endpoint + "/json")
    selected = None
    for item in metadata.get("urls", []):
        filename = str(item.get("filename", ""))
        if filename.endswith("-py3-none-any.whl"):
            selected = item
            break
    if not selected:
        raise ValueError(
            f"{name} has no universal Python wheel; a runtime-locked ZABAWHEELS build is required"
        )
    digest = (selected.get("digests") or {}).get("sha256", "")
    return {
        "name": name,
        "version": str((metadata.get("info") or {}).get("version", version or "")),
        "runtime_id": "py3-none-any",
        "abi": "any",
        "channel": "pypi",
        "artifact": {
            "filename": selected["filename"],
            "url": selected["url"],
            "size": int(selected.get("size", 0)),
            "sha256": digest,
        },
        "dependencies": list((metadata.get("info") or {}).get("requires_dist") or []),
        "native": {"has_extensions": False, "needed_libraries": []},
        "source": {"url": endpoint, "sha256": "", "license": (metadata.get("info") or {}).get("license", "")},
    }


def resolve(name: str, version: str = None, channel: str = "stable") -> dict:
    normalized = canonicalize(name)
    curated = _manifest_from_curated(normalized, channel)
    if curated:
        return curated
    return _manifest_from_pypi(normalized, version)


def _catalog_cache_path(runtime_id: str, abi: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{runtime_id}-{abi}")
    return CACHE_DIR / "catalogs" / f"{safe}.json"


def _fetch_curated_catalog() -> tuple:
    """Return ``(packages, status)`` for this runtime's curated catalog.

    Status is one of ``live`` (just fetched), ``cache`` (fresh on-disk copy),
    ``stale`` (expired on-disk copy, network unavailable or offline), or
    ``unavailable`` (nothing usable anywhere). The status is surfaced to the
    user verbatim — a stale answer is fine as long as it is labeled as one.
    """
    fp = runtime_fingerprint()
    cache_path = _catalog_cache_path(fp["runtime_id"], fp["android"]["abi"])
    if not _offline():
        try:
            url = f"{INDEX_URL}/runtimes/{fp['runtime_id']}/{fp['android']['abi']}.json"
            payload = _request_json(url, timeout=SEARCH_TIMEOUT)
            packages = payload.get("packages")
            if not isinstance(packages, dict):
                raise ValueError("curated catalog has no packages object")
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps({"fetched_at": time.time(), "packages": packages}),
                    encoding="utf-8",
                )
            except OSError:
                pass  # caching is best-effort; the fresh result still counts
            return packages, "live"
        except Exception:
            pass  # fall through to whatever the cache holds
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        packages = cached.get("packages")
        if isinstance(packages, dict):
            age = time.time() - float(cached.get("fetched_at", 0) or 0)
            return packages, "cache" if age <= CATALOG_TTL_SECONDS else "stale"
    except (OSError, ValueError):
        pass
    return {}, "unavailable"


def _probe_pypi(name: str) -> dict:
    """Exact-name probe against the PyPI JSON API.

    PyPI has no search API any more (the XML-RPC search was shut down years
    ago), so the honest thing zpip can offer is an *exact* lookup — the same
    data ``zpip install`` would resolve for an uncurated name.
    """
    try:
        url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json"
        metadata = _request_json(url, timeout=SEARCH_TIMEOUT)
    except Exception:
        return None
    info = metadata.get("info") or {}
    return {
        "name": str(info.get("name") or name),
        "version": str(info.get("version") or ""),
        "summary": str(info.get("summary") or ""),
    }


def _import_name(name: str) -> str:
    """Best-effort guess of the import name from the distribution name.

    Only a fallback. The authoritative answer lives *inside* the artifact and
    is read by :func:`_discover_modules`; guessing is wrong for a large class
    of real packages (``markdown-it-py`` imports ``markdown_it``,
    ``python-dateutil`` imports ``dateutil``).
    """
    aliases = {
        "beautifulsoup4": "bs4", "pillow": "PIL", "python-dotenv": "dotenv",
        "pyyaml": "yaml", "pyjwt": "jwt",
    }
    return aliases.get(name, name.replace("-", "_"))


def _modules_from_paths(paths, package: str) -> list:
    """Derive importable top-level names from a list of archive/relative paths.

    Skips ``.dist-info``/``.data`` metadata trees, private names and anything
    that is not a valid identifier. Returns packages (directories with
    ``__init__.py``) and top-level modules (``foo.py``), preferring a name
    that resembles the distribution so the smoke test stays meaningful.
    """
    packages: set = set()
    modules: set = set()
    for raw in paths:
        parts = PurePosixPath(raw).parts
        if not parts:
            continue
        head = parts[0]
        if head.endswith((".dist-info", ".data")) or head.startswith("."):
            continue
        if len(parts) > 1:
            if parts[1] == "__init__.py" and head.isidentifier():
                packages.add(head)
        elif head.endswith(".py"):
            stem = head[:-3]
            if stem.isidentifier() and stem != "__init__":
                modules.add(stem)
    candidates = sorted(packages) + sorted(modules - packages)
    if not candidates:
        return []
    # Prefer a candidate related to the distribution name; several packages
    # ship helper top-levels (attrs ships both `attr` and `attrs`).
    normalised = package.replace("-", "_").replace(".", "_").lower()
    for candidate in candidates:
        if candidate.lower() == normalised:
            return [candidate, *[c for c in candidates if c != candidate]]
    for candidate in candidates:
        low = candidate.lower()
        if normalised.startswith(low) or low.startswith(normalised):
            return [candidate, *[c for c in candidates if c != candidate]]
    return candidates


def _discover_modules(staging: Path, package: str) -> list:
    """Importable names actually provided by an unpacked wheel.

    Reads, in order of authority:

    1. ``*.dist-info/top_level.txt`` — the packaging tool's own declaration;
    2. the extracted tree, if that file is absent (it is optional and modern
       wheels frequently omit it).

    Falls back to :func:`_import_name` only when the artifact reveals nothing.
    """
    try:
        for info in staging.glob("*.dist-info"):
            top_level = info / "top_level.txt"
            if top_level.is_file():
                declared = [
                    line.strip()
                    for line in top_level.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip() and line.strip().isidentifier()
                ]
                if declared:
                    ordered = _modules_from_paths(
                        [f"{name}/__init__.py" for name in declared], package
                    )
                    return ordered or declared
    except OSError:
        pass
    try:
        relative = [
            str(path.relative_to(staging))
            for path in staging.rglob("*")
            if path.is_file()
        ]
    except OSError:
        relative = []
    return _modules_from_paths(relative, package) or [_import_name(package)]


def _smoke_test_in_process(staging: Path, module: str) -> None:
    old_path = list(sys.path)
    old_modules = set(sys.modules.keys())
    try:
        if str(staging) not in sys.path:
            sys.path.insert(0, str(staging))
        if str(USER_PACKAGES_DIR) not in sys.path:
            sys.path.insert(1, str(USER_PACKAGES_DIR))
        importlib.invalidate_caches()
        importlib.import_module(module)
    finally:
        sys.path[:] = old_path
        for mod in list(sys.modules.keys()):
            if mod not in old_modules:
                sys.modules.pop(mod, None)


def _smoke_test(staging: Path, package: str, module: str | None = None) -> None:
    # Read the real top-level name out of the artifact; guessing from the
    # distribution name breaks for markdown-it-py, python-dateutil and many
    # others, which failed installs that would otherwise have succeeded.
    module = module or _discover_modules(staging, package)[0]
    if _is_android():
        _smoke_test_in_process(staging, module)
        return
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(staging), str(USER_PACKAGES_DIR), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise ValueError(f"Smoke import failed: {(result.stderr or result.stdout).strip()}")
    except (OSError, PermissionError, FileNotFoundError):
        _smoke_test_in_process(staging, module)


def _manifest_requirements(manifest: dict) -> list:
    requirements = []
    for dep in manifest.get("dependencies") or []:
        try:
            requirement = Requirement(dep)
        except InvalidRequirement:
            continue
        if requirement.marker and not requirement.marker.evaluate():
            continue
        requirements.append(requirement)
    return requirements


def _satisfies(record: Any, requirement) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        return not requirement.specifier or Version(str(record.get("version", ""))) in requirement.specifier
    except InvalidVersion:
        return False


def install(
    name: str,
    version: str = None,
    channel: str = "stable",
    _stack: tuple = (),
) -> dict:
    try:
        package = canonicalize(name)
    except (TypeError, ValueError) as error:
        return {"ok": False, "package": "", "error": str(error), "rolled_back": False}
    if package in _stack:
        chain = " -> ".join((*_stack, package))
        return {"ok": False, "package": package, "error": f"Dependency cycle: {chain}", "rolled_back": True}
    transaction = uuid.uuid4().hex
    staging = STAGING_DIR / transaction
    backup = STAGING_DIR / f"{transaction}.backup"
    wheel = DOWNLOADS_DIR / f"{transaction}.whl"
    committed = []
    previous = []
    new_dependencies = []
    try:
        manifest = resolve(package, version, channel)
        db_before = _load_db()
        for requirement in _manifest_requirements(manifest):
            dependency = canonicalize(requirement.name)
            existing = db_before.get(dependency)
            if _satisfies(existing, requirement):
                continue
            if existing:
                raise ValueError(
                    f"Dependency conflict: installed {dependency} {existing.get('version')} "
                    f"does not satisfy {requirement.specifier}; upgrade it explicitly"
                )
            exact = None
            try:
                specs = list(requirement.specifier)
                if len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version:
                    exact = specs[0].version
            except Exception:
                # Unusual specifier objects (fallback Requirement shim) simply
                # mean "no exact pin" — never swallow KeyboardInterrupt/SystemExit.
                pass
            dependency_result = install(dependency, exact, channel, (*_stack, package))
            if not dependency_result.get("ok"):
                raise ValueError(
                    f"Dependency {dependency} failed: {dependency_result.get('error', 'unknown error')}"
                )
            new_dependencies.append(dependency)
            installed = _load_db().get(dependency)
            if not _satisfies(installed, requirement):
                raise ValueError(
                    f"Resolved {dependency} {installed.get('version') if installed else ''} "
                    f"does not satisfy {requirement.specifier}"
                )
        artifact = manifest.get("artifact") or {}
        wheel.parent.mkdir(parents=True, exist_ok=True)
        label = f"{package}-{manifest.get('version', '')}".rstrip("-")
        size, digest = _download(
            str(artifact.get("url", "")), str(artifact.get("sha256", "")), wheel, label
        )
        staging.mkdir(parents=True)
        files = _extract(wheel, staging)
        modules = _discover_modules(staging, package)
        _smoke_test(staging, package, modules[0])

        db = _load_db()
        old = db.get(package) if isinstance(db.get(package), dict) else {}
        previous = list(old.get("files", []))
        owners = {
            relative: owner
            for owner, record in db.items()
            if owner != package and isinstance(record, dict)
            for relative in record.get("files", [])
        }
        collisions = sorted(set(files) & set(owners))
        if collisions:
            first = collisions[0]
            raise ValueError(f"File ownership conflict: {first} belongs to {owners[first]}")
        backup.mkdir(parents=True)
        for relative in files:
            source = staging / relative
            target = USER_PACKAGES_DIR / relative
            if target.exists() and target.is_file():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            if relative.endswith(".so"):
                # Android 14+ "safer dynamic code loading": native libraries
                # loaded via dlopen() must be read-only files (warning on
                # targetSdk 34, hard `UnsatisfiedLinkError` on newer targets).
                # Freezing them at install time keeps native wheel imports
                # working — the same fix Termux applied to termux-am (chmod 400).
                # Best-effort: a filesystem without chmod support keeps the
                # previous (writable) behaviour rather than aborting the commit.
                try:
                    os.chmod(target, 0o444)
                except OSError:
                    pass
            committed.append(relative)
        for obsolete in set(previous) - set(files):
            target = USER_PACKAGES_DIR / obsolete
            if target.is_file():
                target.unlink()
        # "explicit" = the user asked for this by name; a package pulled in
        # only as a dependency is implicit (_stack is non-empty for those).
        # Re-installing a dependency by name promotes it to explicit, and a
        # package never silently loses the flag. This is what makes a future
        # `zpip autoremove` possible and what `zpip list` reports.
        previously_explicit = bool(old.get("explicit")) if old else False
        db[package] = {
            "version": manifest.get("version", ""),
            "runtime_id": manifest.get("runtime_id", ""),
            "abi": manifest.get("abi", ""),
            "channel": manifest.get("channel", channel),
            "files": sorted(files),
            "sha256": digest,
            "size": size,
            "transaction": transaction,
            # Recorded from the artifact so `zpip verify` re-imports the name
            # the wheel really provides instead of re-deriving a guess.
            "modules": modules,
            "explicit": previously_explicit or not _stack,
        }
        _save_db(db)
        importlib.invalidate_caches()
        result: dict = {
            "ok": True,
            "package": package,
            "version": manifest.get("version"),
            "files": len(files),
            "sha256": digest,
            "dependencies_installed": new_dependencies,
        }
        # Loudly flag PyPI name collisions with famous CLI tools (nano):
        # installing "succeeded", but almost certainly not what was meant.
        warning = _tool_collision_warning(package)
        if warning:
            result["warning"] = warning
        return result
    except Exception as error:
        for relative in reversed(committed):
            target, saved = USER_PACKAGES_DIR / relative, backup / relative
            if saved.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(saved, target)
            elif target.exists():
                target.unlink()
        for dependency in reversed(new_dependencies):
            uninstall(dependency)
        return {"ok": False, "package": package, "error": str(error), "rolled_back": True}
    finally:
        wheel.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


def uninstall(name: str) -> dict:
    package = canonicalize(name)
    db = _load_db()
    record = db.get(package)
    if not isinstance(record, dict):
        return {"ok": False, "error": f"{package} is not managed by zpip"}
    removed = 0
    for relative in record.get("files", []):
        target = (USER_PACKAGES_DIR / relative).resolve()
        base = USER_PACKAGES_DIR.resolve()
        if base in target.parents and target.is_file():
            target.unlink()
            removed += 1
    for path in sorted(USER_PACKAGES_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    del db[package]
    _save_db(db)
    importlib.invalidate_caches()
    return {"ok": True, "package": package, "removed": removed}


def verify(name: str) -> dict:
    package = canonicalize(name)
    record = _load_db().get(package)
    if not isinstance(record, dict):
        return {"ok": False, "error": f"{package} is not managed by zpip"}
    missing = [item for item in record.get("files", []) if not (USER_PACKAGES_DIR / item).is_file()]
    # Prefer the module names captured from the artifact at install time;
    # only fall back to guessing for records written before they were stored.
    recorded = record.get("modules") or []
    module = recorded[0] if recorded else _import_name(package)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(USER_PACKAGES_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        if _is_android():
            old_path = list(sys.path)
            try:
                if str(USER_PACKAGES_DIR) not in sys.path:
                    sys.path.insert(0, str(USER_PACKAGES_DIR))
                importlib.invalidate_caches()
                importlib.import_module(module)
                import_ok = True
                error = ""
            finally:
                sys.path[:] = old_path
        else:
            result = subprocess.run(
                [sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            import_ok = result.returncode == 0
            error = "" if import_ok else (result.stderr or result.stdout).strip()
    except (OSError, PermissionError, FileNotFoundError):
        old_path = list(sys.path)
        try:
            if str(USER_PACKAGES_DIR) not in sys.path:
                sys.path.insert(0, str(USER_PACKAGES_DIR))
            importlib.invalidate_caches()
            importlib.import_module(module)
            import_ok = True
            error = ""
        except Exception as exc:
            import_ok, error = False, str(exc)
        finally:
            sys.path[:] = old_path
    except Exception as exc:
        import_ok, error = False, str(exc)
    return {"ok": not missing and import_ok, "package": package, "missing": missing, "import_ok": import_ok, "error": error}


def list_installed() -> dict:
    return {"ok": True, "packages": _load_db()}


def doctor() -> dict:
    checks = {name: verify(name) for name in _load_db()}
    fingerprint = runtime_fingerprint()
    free = shutil.disk_usage(str(APP_DIR)).free
    return {
        "ok": all(item.get("ok") for item in checks.values()),
        "runtime": fingerprint,
        "free_bytes": free,
        "index": INDEX_URL,
        "packages": checks,
    }


def info(name: str) -> dict:
    package = canonicalize(name)
    installed = _load_db().get(package)
    try:
        available = resolve(package)
        return {"ok": True, "name": package, "installed": installed, "available": available}
    except Exception as error:
        return {"ok": bool(installed), "name": package, "installed": installed, "error": str(error)}


def search(query: str) -> dict:
    """Search the curated ZABAWHEELS index, the local installs, and PyPI.

    The query may be multiple words; every token must match (AND). There is
    deliberately no hardcoded package list: results come only from the live
    (or cached) curated catalog, the installed database, and — for a single
    token that is a valid package name — an exact PyPI probe. When a source
    cannot be reached the result says so instead of inventing answers.
    """
    raw = str(query or "")
    tokens = _QUERY_TOKEN.findall(raw.lower())
    if not tokens:
        return {"ok": False, "query": raw, "error": "empty search query"}

    installed = _load_db()
    catalog, curated_status = _fetch_curated_catalog()

    def _norm(name: str) -> str:
        return re.sub(r"[-_.]+", "-", str(name).lower())

    norm_tokens = [_norm(token) for token in tokens]

    def _rank(name: str, summary: str):
        norm_name = _norm(name)
        summary_l = str(summary or "").lower()
        for token, norm_token in zip(tokens, norm_tokens):
            if norm_token not in norm_name and token not in summary_l:
                return None
        if len(norm_tokens) == 1 and norm_name == norm_tokens[0]:
            return 0
        if all(norm_token in norm_name for norm_token in norm_tokens):
            return 1
        return 2

    hits = {}
    # On a tie the richer source wins: curated metadata beats a bare PyPI
    # record, which beats an installed-db row (name and version only).
    _SOURCE_PRIORITY = {"curated": 0, "pypi": 1, "installed": 2}

    def _consider(name, *, version="", summary="", source):
        rank = _rank(name, summary)
        if rank is None:
            return
        key = _norm(name)
        weight = (rank, _SOURCE_PRIORITY[source])
        entry = hits.get(key)
        if entry is None or weight < (entry[0], entry[1]):
            hits[key] = (rank, _SOURCE_PRIORITY[source], {
                "name": str(name),
                "version": str(version or ""),
                "summary": str(summary or ""),
                "source": source,
                "installed": key in installed,
            })

    for pkg_name, manifest in catalog.items():
        if isinstance(manifest, dict):
            _consider(
                manifest.get("name", pkg_name),
                version=manifest.get("version", ""),
                summary=manifest.get("summary", ""),
                source="curated",
            )
    for pkg_name, record in installed.items():
        _consider(
            pkg_name,
            version=(record or {}).get("version", "") if isinstance(record, dict) else "",
            source="installed",
        )

    pypi_status = "skipped"
    exact_curated = len(norm_tokens) == 1 and norm_tokens[0] in {_norm(k) for k in catalog}
    if len(tokens) == 1 and _NAME.fullmatch(tokens[0]) and not _offline() and not exact_curated:
        probe = _probe_pypi(tokens[0])
        if probe is not None:
            _consider(**probe, source="pypi")
            pypi_status = "live"
        else:
            pypi_status = "unavailable"

    results = [
        entry
        for _, _, entry in sorted(hits.values(), key=lambda item: (item[0], item[2]["name"]))
    ]
    return {
        "ok": True,
        "query": raw,
        "results": results,
        "sources": {"curated": curated_status, "pypi": pypi_status},
    }


def format_output(command, result):
    """Turn a structured zpip result into terminal-friendly text.

    Shared by the REST server (zmux.server) and the CLI (zmux.cli) so a zpip
    invocation renders identically no matter which transport carried it.

    Returns a ``(text, exit_code)`` tuple.
    """
    parts = command.strip().split()
    action = parts[1] if len(parts) > 1 else ""

    if action == "verify":
        pkg = result.get("package", parts[2] if len(parts) > 2 else "")
        if result.get("ok"):
            return f"{pkg} is installed and verified", 0
        missing = result.get("missing", [])
        if missing:
            return f"{pkg} verification failed: missing files: {', '.join(missing)}", 1
        error = result.get("error", "unknown error")
        return f"{pkg} verification failed: {error}", 1

    if action == "doctor" and "runtime" in result:
        rt = result.get("runtime", {})
        free = result.get("free_bytes", 0)
        pkgs = result.get("packages", {})
        idx = result.get("index", "unknown")
        lines = [
            "ZMUX Package Manager",
            f"Runtime: {rt.get('runtime_id', 'unknown')}",
            f"Python: {rt.get('python', {}).get('version', 'unknown')}",
            f"ABI: {rt.get('android', {}).get('abi', 'unknown')}",
            f"User packages: {rt.get('paths', {}).get('user_packages', 'unknown')}",
            f"Free storage: {free:,} bytes",
            f"Index: {idx}",
        ]
        if pkgs:
            lines.extend(("", "Installed packages:"))
            for name, status in pkgs.items():
                lines.append(f"  {name}: {'OK' if status.get('ok') else 'FAILED'}")
        return "\n".join(lines), 0 if result.get("ok") else 1

    if not result.get("ok"):
        return f"Error: {result.get('error', 'unknown error')}", 1

    if action == "install":
        pkg = result.get("package", "")
        ver = result.get("version", "")
        deps = result.get("dependencies_installed", [])
        msg = f"Successfully installed {pkg}{f'-{ver}' if ver else ''}"
        if deps:
            msg += f"\nAlso installed: {', '.join(deps)}"
        warning = result.get("warning")
        if warning:
            msg += f"\n\nWARNING: {warning}"
        return msg, 0
    if action == "list":
        pkgs = result.get("packages", {})
        if pkgs:
            lines = []
            for name, record in pkgs.items():
                version = record.get("version", "")
                # Mark dependencies so users can tell what they asked for
                # from what was pulled in underneath it.
                suffix = "" if record.get("explicit", True) else "  (dependency)"
                lines.append(f"{name} {version}{suffix}")
            return "\n".join(lines), 0
        return "No packages installed", 0
    if action == "search":
        results = result.get("results", [])
        lines = []
        for entry in results:
            tag = entry.get("source", "?")
            if entry.get("installed") and entry.get("source") != "installed":
                tag += ",installed"
            version = f" {entry['version']}" if entry.get("version") else ""
            summary = f" - {entry['summary']}" if entry.get("summary") else ""
            lines.append(f"{entry.get('name', '?')}{version} [{tag}]{summary}")
        if not results:
            lines.append("No packages found")
        sources = result.get("sources", {})
        # Unavailable/degraded sources are disclosed even on an empty screen:
        # "no results" because the index is unreachable must never read as
        # "the package does not exist".
        if sources.get("curated") == "stale":
            lines.append("(curated index: stale cache; connect to refresh)")
        elif sources.get("curated") == "unavailable":
            lines.append("(curated index unavailable; no cached copy)")
        if sources.get("pypi") == "unavailable":
            lines.append("(pypi.org unreachable; exact-match probe skipped)")
        return "\n".join(lines), 0
    if action == "info":
        pkg = result.get("name", "")
        installed = result.get("installed")
        available = result.get("available")
        lines = [f"Package: {pkg}"]
        if installed:
            lines.append(f"Installed: {installed.get('version', 'unknown')}")
        if available:
            lines.append(f"Available: {available.get('version', 'unknown')}")
        return "\n".join(lines), 0
    if action == "uninstall":
        return f"Successfully uninstalled {result.get('package', '')}", 0
    return "Command executed successfully", 0


def format_fingerprint(fp: dict) -> str:
    """Render the runtime fingerprint for the ``zmux-info`` command.

    Shared by the REST server (zmux.server) and the CLI (zmux.cli).
    """
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
        f"User packages:      {fp['paths']['user_packages']}",
        f"Free storage:       {fp['storage']['free_bytes']:,} bytes",
    ]
    installed = fp["installed"]
    if installed:
        lines.append(f"Installed packages: {', '.join(installed)}")
    else:
        lines.append("Installed packages: (none)")
    return "\n".join(lines)


# Runtime-info compatibility aliases; diagnostics live outside legacy zpip.
from zmux.runtime_info import android_abi, format_fingerprint, runtime_fingerprint


def dispatch(command: str) -> dict:
    """Dispatch one honest zpip command without invoking a shell."""
    try:
        args = shlex.split(command)
    except ValueError as error:
        return _mark_legacy_result({"ok": False, "error": str(error)})
    if args and args[0] == "zpip":
        args.pop(0)
    if not args:
        return _mark_legacy_result({"ok": False, "error": "usage: zpip search|info|install|list|verify|uninstall|doctor"})
    action, values = args[0], args[1:]
    try:
        if action == "list" and not values:
            return _mark_legacy_result(list_installed())
        if action == "doctor" and not values:
            return _mark_legacy_result(doctor())
        if action == "search" and values:
            return _mark_legacy_result(search(" ".join(values)))
        if action in {"info", "verify", "uninstall"} and len(values) == 1:
            return _mark_legacy_result(globals()[action](values[0]))
        if action == "install" and len(values) in {1, 2}:
            return _mark_legacy_result(install(values[0], values[1] if len(values) == 2 else None))
    except (ValueError, OSError) as error:
        return _mark_legacy_result({"ok": False, "error": str(error)})
    return _mark_legacy_result({"ok": False, "error": f"invalid arguments for zpip {action}"})
