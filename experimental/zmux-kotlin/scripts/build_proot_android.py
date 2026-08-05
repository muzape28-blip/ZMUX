#!/usr/bin/env python3
"""Cross-compile PRoot + talloc for Android (armeabi-v7a / arm64-v8a).

Adapted from Kai 9000's ``build-proot.sh`` (https://github.com/SimonSchubert/Kai,
Apache-2.0) with the logic kept identical so the same artefacts are produced:

    libproot.so            the PRoot binary, renamed lib*.so so Android
                           extracts it into nativeLibraryDir (W^X-safe exec)
    libproot-loader.so     PRoot's loader ELF (PROOT_LOADER at runtime)
    libproot-loader32.so   the 32-bit loader (arm64-v8a builds only)
    libtalloc.so           talloc, PRoot's allocator dependency, with its
                           SONAME/DT_NEEDED rewritten from libtalloc.so.2 to
                           libtalloc.so so Android's linker (exact-filename
                           matching) can resolve it from nativeLibraryDir

Sources are pinned and re-downloaded on every build (never vendored):
    PRoot   termux/proot @ 4dba3afbf3a63af89b4d9c1a59bf2bda10f4d10f
            (the exact commit Kai pins)
    talloc  deepin-community/talloc @ 2.4.2-1deepin1 (full upstream source
            mirror of talloc 2.4.2, Samba waf build)

Usage:
    python scripts/build_proot_android.py \
        --ndk ~/.buildozer/android/platform/android-ndk-r28c \
        --out app/libs \
        --abis armeabi-v7a,arm64-v8a

Licensing: PRoot is GPL-2.0+ (STMicroelectronics), talloc is LGPL-3.0,
talloc's waf build is Samba's (GPL-3.0 with the runtime exception); this
script only orchestrates their source builds.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PROOT_COMMIT = "4dba3afbf3a63af89b4d9c1a59bf2bda10f4d10f"
TALLOC_TAG = "2.4.2-1deepin1"
MIN_API = 26
ABI_TRIPLES = {
    "armeabi-v7a": "armv7a-linux-androideabi",
    "arm64-v8a": "aarch64-linux-android",
}
LOADER32_ABI = {"arm64-v8a": "armeabi-v7a", "armeabi-v7a": None}

#: talloc's Samba build records SONAME/DT_NEEDED as "libtalloc.so.2". Android's
#: linker resolves DT_NEEDED entries against *exact* filenames (it only retries
#: with a ".so" suffix for names that already end in ".so"), and Android Gradle
#: jniLibs packaging does not reliably keep files whose names do not end in
#: ".so". Shipping "libtalloc.so.2" therefore made libproot.so die at exec time
#: on the phone:
#:
#:     CANNOT LINK EXECUTABLE ".../lib/arm/libproot.so":
#:     library "libtalloc.so.2" not found: needed by main executable
#:
#: Fix: rewrite the string in place to plain "libtalloc.so" and package the
#: library under that name.
#:
#: CRITICAL: the replacement MUST be exactly the same byte length as the
#: needle. "libtalloc.so" is 12 chars; a naive replacement of
#: "libtalloc.so.2" (14 bytes) with b"libtalloc.so\\x00" (13 bytes) shrinks the
#: file by one byte, shifting every section/program header and string after
#: it — the linker then reads a misaligned .dynamic (garbage DT entries) and
#: fails with "empty/missing DT_HASH/DT_GNU_HASH" (this exact bug shipped once
#: and failed on-device). The two-NUL form keeps the length identical.
TALLOC_NEEDED = b"libtalloc.so.2"                  # 14 bytes
TALLOC_NEEDED_REPLACEMENT = b"libtalloc.so\x00\x00"  # 14 bytes — must match!

assert len(TALLOC_NEEDED) == len(TALLOC_NEEDED_REPLACEMENT), (
    "talloc NEEDED rewrite must preserve byte length"
)

#: Libraries the Android bionic linker always provides; DT_NEEDED entries for
#: these must not be packaged next to proot.
ANDROID_SYSTEM_LIBS = {
    "libc.so", "libm.so", "libdl.so", "liblog.so", "libpthread.so",
    "libstdc++.so",
}

_NEEDED_RE = re.compile(r"Shared library: \[([^\]]+)\]")

CROSS_ANSWERS = """Checking uname sysname type: "Linux"
Checking uname machine type: "dontcare"
Checking uname release type: "dontcare"
Checking uname version type: "dontcare"
Checking simple C program: OK
building library support: OK
Checking for large file support: OK
Checking for -D_FILE_OFFSET_BITS=64: OK
Checking for WORDS_BIGENDIAN: OK
Checking for C99 vsnprintf: OK
Checking for HAVE_SECURE_MKSTEMP: OK
rpath library support: OK
-Wl,--version-script support: FAIL
Checking correct behavior of strtoll: OK
Checking correct behavior of strptime: OK
Checking for HAVE_IFACE_GETIFADDRS: OK
Checking for HAVE_IFACE_IFCONF: OK
Checking for HAVE_IFACE_IFREQ: OK
Checking getconf LFS_CFLAGS: OK
Checking for large file support without additional flags: OK
Checking for working strptime: OK
Checking for HAVE_SHARED_MMAP: OK
Checking for HAVE_MREMAP: OK
Checking for HAVE_INCOHERENT_MMAP: OK
Checking getconf large file support flags work: OK
"""


def run(cmd: list, cwd: Path | None = None, env: dict | None = None) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=True)


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)
    return dest


def extract(archive: Path, dest: Path) -> Path:
    import tarfile
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest)
    members = list(dest.iterdir())
    if len(members) == 1 and members[0].is_dir():
        return members[0]
    return dest


def find_ndk(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    for key in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_HOME"):
        value = os.environ.get(key)
        if value:
            candidate = Path(value)
            if (candidate / "toolchains" / "llvm").is_dir():
                return candidate
    for base in (Path.home() / ".buildozer" / "android" / "platform",
                 Path.home() / "Android" / "Sdk", Path.home() / "Android" / "sdk"):
        if base.is_dir():
            for candidate in sorted(base.glob("android-ndk-*"), reverse=True):
                if (candidate / "toolchains" / "llvm").is_dir():
                    return candidate
    raise SystemExit("NDK not found: pass --ndk or set ANDROID_NDK_HOME")


def toolchain(ndk: Path) -> Path:
    for tag in (f"{sys.platform}-x86_64", "linux-x86_64", "darwin-x86_64"):
        candidate = ndk / "toolchains" / "llvm" / "prebuilt" / tag / "bin"
        if candidate.is_dir():
            return candidate
    raise SystemExit(f"no llvm prebuilt toolchain under {ndk}")


def build_talloc(src: Path, tc: Path, abi: str, sysroot: Path, jobs: int) -> None:
    triple = ABI_TRIPLES[abi]
    cc = tc / f"{triple}{MIN_API}-clang"
    prefix = sysroot
    if (prefix / "lib" / "libtalloc.so").is_file():
        print(f"[talloc/{abi}] already built, skipping")
        return
    build = src / f"build-talloc-{abi}"
    if build.exists():
        shutil.rmtree(build)
    shutil.copytree(src, build)
    answers = build / "cross-answers.txt"
    answers.write_text(CROSS_ANSWERS, encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "CC": str(cc),
        "AR": str(tc / "llvm-ar"),
        "RANLIB": str(tc / "llvm-ranlib"),
        "STRIP": str(tc / "llvm-strip"),
    })
    run(["./configure", "--prefix", str(prefix), "--disable-rpath",
         "--disable-python", "--cross-compile", "--cross-answers=cross-answers.txt"],
        cwd=build, env=env)
    run(["make", "-j", str(jobs)], cwd=build, env=env)
    run(["make", "install"], cwd=build, env=env)


def build_proot(src: Path, tc: Path, abi: str, sysroot: Path, jobs: int) -> tuple:
    triple = ABI_TRIPLES[abi]
    cc = tc / f"{triple}{MIN_API}-clang"
    build = src / f"build-proot-{abi}"
    if build.exists():
        shutil.rmtree(build)
    shutil.copytree(src, build)
    # Remove HAS_LOADER_32BIT from arch.h — the 32-bit loader is built
    # separately with the 32-bit toolchain (NDK clang has no -m32).
    arch_h = build / "src" / "arch.h"
    text = arch_h.read_text(encoding="utf-8")
    arch_h.write_text(
        "\n".join(line for line in text.splitlines()
                  if "HAS_LOADER_32BIT" not in line) + "\n",
        encoding="utf-8",
    )
    tmp_bin = build / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)
    if not (tmp_bin / "readelf").exists():
        (tmp_bin / "readelf").symlink_to(tc / "llvm-readelf")
    loader_dir = build / "loader-out"
    env = dict(os.environ)
    env.update({
        "CC": str(cc),
        "LD": str(cc),
        "STRIP": str(tc / "llvm-strip"),
        "OBJCOPY": str(tc / "llvm-objcopy"),
        "OBJDUMP": str(tc / "llvm-objdump"),
        "CFLAGS": (f"-DARG_MAX=131072 -I{sysroot}/include "
                   "-Wno-error=implicit-function-declaration -Wno-error=int-conversion"),
        "LDFLAGS": f"-L{sysroot}/lib",
        "PATH": f"{tmp_bin}:{os.environ.get('PATH', '')}",
    })
    run(["make", "-C", "src", f"PROOT_UNBUNDLE_LOADER={loader_dir.relative_to(build)}",
         "-j", str(jobs)], cwd=build, env=env)
    out = build / "out"
    out.mkdir(parents=True, exist_ok=True)
    # PROOT_UNBUNDLE_LOADER emits the loader as a separate file; keep the
    # runtime loader from src/loader/loader (the one the app references via
    # PROOT_LOADER). Mirrors Kai exactly.
    # PROOT_UNBUNDLE_LOADER can leave the loader in either
    # src/loader/loader (bundled build) or src/loader-out/loader
    # (unbundled). Try the known candidates and fail with a clear message.
    candidates = [
        build / "src" / "loader" / "loader",
        build / "src" / "loader-out" / "loader",
    ]
    for name, src_file in (
        ("libproot.so", build / "src" / "proot"),
    ):
        if not src_file.is_file():
            raise SystemExit(f"build did not produce {src_file}")
        shutil.copy2(src_file, out / name)
    loader_src = next((c for c in candidates if c.is_file()), None)
    if loader_src is None:
        raise SystemExit(
            "build did not produce a loader ELF "
            f"(looked in: {', '.join(str(c) for c in candidates)})"
        )
    shutil.copy2(loader_src, out / "libproot-loader.so")
    shutil.copy2(sysroot / "lib" / "libtalloc.so", out / "libtalloc.so")
    for name in ("libproot.so", "libproot-loader.so", "libtalloc.so"):
        run([str(tc / "llvm-strip"), str(out / name)])
    return build, out, loader_dir


def build_loader32(proot_build: Path, tc: Path, abi: str, out: Path) -> None:
    abi32 = LOADER32_ABI[abi]
    if not abi32:
        return
    triple32 = ABI_TRIPLES[abi32]
    cc32 = tc / f"{triple32}{MIN_API}-clang"
    src_dir = proot_build / "src"
    # LOADER_ADDRESS for the 32-bit arch, read via the C preprocessor.
    probe = subprocess.run(
        [str(cc32), "-dM", "-E", "-x", "c", str(src_dir / "arch.h")],
        capture_output=True, text=True, check=True,
    )
    addr = None
    for line in probe.stdout.splitlines():
        if "LOADER_ADDRESS" in line:
            addr = line.split()[-1]
            break
    if not addr:
        print(f"[loader32/{abi}] WARNING: no LOADER_ADDRESS, skipping")
        return
    print(f"[loader32/{abi}] LOADER_ADDRESS={addr}", flush=True)
    obj = proot_build / f"loader32-{abi}.o"
    asm = proot_build / f"assembly32-{abi}.o"
    run([str(cc32), f"-DLOADER_ADDRESS={addr}", f"-I{src_dir}",
         "-Wall", "-Wextra", "-O2", "-fPIC", "-ffreestanding",
         "-c", str(src_dir / "loader" / "loader.c"), "-o", str(obj)])
    run([str(cc32), f"-I{src_dir}", "-c",
         str(src_dir / "loader" / "assembly.S"), "-o", str(asm)])
    run([str(cc32), "-static", "-nostdlib", "-Wl,-N", f"-Wl,-Ttext={addr}",
         "-o", str(out / "libproot-loader32.so"), str(obj), str(asm)])
    run([str(tc / "llvm-strip"), str(out / "libproot-loader32.so")])


def patch_talloc_names(out: Path) -> None:
    """Rewrite talloc's SONAME/DT_NEEDED strings to plain ``libtalloc.so``.

    ``libproot.so`` links against talloc, so its ``.dynstr`` carries the
    NEEDED string ``libtalloc.so.2`` (talloc's SONAME), and ``libtalloc.so``
    carries the same string as its own SONAME. Android's linker matches
    DT_NEEDED against exact filenames, so both are rewritten in place to
    ``libtalloc.so``.

    The replacement is length-preserving (see TALLOC_NEEDED_REPLACEMENT) —
    any size change would silently corrupt the whole ELF. This is enforced
    here as a hard invariant: if the file size ever changes, the build dies
    instead of shipping a broken binary.
    """
    for name in ("libproot.so", "libtalloc.so"):
        path = out / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        count = data.count(TALLOC_NEEDED)
        if count == 0:
            continue
        patched = data.replace(TALLOC_NEEDED, TALLOC_NEEDED_REPLACEMENT)
        if len(patched) != len(data):
            raise SystemExit(
                f"[patch] {name}: FAILED — replacement changed the file size "
                f"({len(data)} -> {len(patched)} bytes). The talloc NEEDED "
                "rewrite must preserve byte length; refusing to ship a "
                "corrupted binary."
            )
        path.write_bytes(patched)
        print(f"[patch] {name}: rewrote {count}× 'libtalloc.so.2' -> "
              f"'libtalloc.so' (SONAME/DT_NEEDED)", flush=True)


def verify_needed(out: Path, tc: Path) -> None:
    """Fail the build if any packaged .so is broken or needs a missing library.

    Uses ``llvm-readelf`` so the check runs on the exact artifacts CI packs
    into the APK. Two failure classes are caught here instead of on the
    user's phone:

    1. Unresolvable DT_NEEDED (soname/version drift, e.g. talloc 3.x
       recording ``libtalloc.so.3``).
    2. A file that no longer parses — the classic symptom of a non
       length-preserving string rewrite (a one-byte shrink shifts every
       section header, producing "empty/missing DT_HASH/DT_GNU_HASH" and
       garbage DT entries at exec time).
    """
    shipped = {p.name for p in out.glob("*.so")}
    problems: list = []
    for lib in sorted(out.glob("*.so")):
        try:
            result = subprocess.run(
                [str(tc / "llvm-readelf"), "-d", str(lib)],
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError:
            # No .dynamic section (static ELF — the loaders): nothing to check.
            continue
        for line in result.stdout.splitlines():
            match = _NEEDED_RE.search(line)
            if match is None:
                continue
            needed = match.group(1)
            if needed in shipped or needed in ANDROID_SYSTEM_LIBS:
                continue
            problems.append(
                f"{lib.name} needs {needed}, but only {sorted(shipped)} are packaged"
            )
    if problems:
        raise SystemExit(
            "Unresolvable DT_NEEDED in proot artifacts:\n  "
            + "\n  ".join(problems)
        )
    # Belt-and-braces: the exact talloc binding must be the plain name, and
    # the files must parse cleanly (a corrupted rewrite would show up here).
    # libproot.so *needs* libtalloc.so (DT_NEEDED); libtalloc.so *is* the
    # library, so its binding is its SONAME, not a NEEDED entry.
    proot_path = out / "libproot.so"
    if proot_path.is_file():
        result = subprocess.run(
            [str(tc / "llvm-readelf"), "-d", str(proot_path)],
            capture_output=True, text=True, check=True,
        )
        needed = _NEEDED_RE.findall(result.stdout)
        talloc_entry = next((n for n in needed if n.startswith("libtalloc")), None)
        if talloc_entry != "libtalloc.so":
            raise SystemExit(
                f"[verify] libproot.so talloc binding is {talloc_entry!r}, "
                "expected 'libtalloc.so' — the rewrite did not apply"
            )
    talloc_path = out / "libtalloc.so"
    if talloc_path.is_file():
        result = subprocess.run(
            [str(tc / "llvm-readelf"), "-d", str(talloc_path)],
            capture_output=True, text=True, check=True,
        )
        soname_match = re.search(r"SONAME.*\[([^\]]+)\]", result.stdout)
        soname = soname_match.group(1) if soname_match else None
        if soname != "libtalloc.so":
            raise SystemExit(
                f"[verify] libtalloc.so SONAME is {soname!r}, "
                "expected 'libtalloc.so' — the rewrite did not apply"
            )
    print(f"[verify] all DT_NEEDED entries of {sorted(shipped)} resolve; "
          f"talloc binding is 'libtalloc.so'", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ndk", default=None, help="Android NDK root")
    parser.add_argument("--out", default="app/libs", help="output dir")
    parser.add_argument("--abis", default="armeabi-v7a,arm64-v8a")
    parser.add_argument("--work", default=None, help="source/work dir (default: temp)")
    parser.add_argument("--jobs", type=int, default=0)
    args = parser.parse_args()

    ndk = find_ndk(args.ndk)
    tc = toolchain(ndk)
    abis = [a for a in args.abis.split(",") if a]
    jobs = args.jobs or (os.cpu_count() or 4)
    out_root = Path(args.out)
    print(f"NDK: {ndk}\ntoolchain: {tc}\nABIs: {abis}\nout: {out_root}")

    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="zmux-proot-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        proot_src = extract(
            download(f"https://codeload.github.com/termux/proot/tar.gz/{PROOT_COMMIT}",
                     work / "proot.tar.gz"),
            work / "proot-src",
        )
        talloc_src = extract(
            download(f"https://codeload.github.com/deepin-community/talloc/tar.gz/refs/tags/{TALLOC_TAG}",
                     work / "talloc.tar.gz"),
            work / "talloc-src",
        )
        for abi in abis:
            sysroot = work / f"sysroot-{abi}"
            print(f"\n=== [{abi}] talloc ===", flush=True)
            build_talloc(talloc_src, tc, abi, sysroot, jobs)
            print(f"=== [{abi}] proot ===", flush=True)
            build, out, _loader_dir = build_proot(proot_src, tc, abi, sysroot, jobs)
            build_loader32(build, tc, abi, out)
            # Reproducible binaries: drop the .comment section that embeds
            # the NDK clang version string (Kai does the same for F-Droid).
            for lib in out.glob("*.so"):
                run([str(tc / "llvm-objcopy"), "--remove-section", ".comment", str(lib)])
            # On-device "libtalloc.so.2 not found" fix: make talloc's recorded
            # names match the filename Android can actually load, then prove
            # every DT_NEEDED entry resolves among the packaged files.
            patch_talloc_names(out)
            verify_needed(out, tc)
            target = out_root / abi
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(out, target)
            print(f"[{abi}] -> {target}:", flush=True)
            for lib in sorted(target.iterdir()):
                print(f"    {lib.name}  {lib.stat().st_size:,} bytes", flush=True)
        print("\nAll builds complete.")
        return 0
    finally:
        if not args.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
