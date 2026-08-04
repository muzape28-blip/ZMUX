# ZMUX Alpine Linux Sandbox (PRoot) — Engineering Plan

**Status (2026-07-31):** core mechanics proven on Linux (proot built from the
same pinned source Kai 9000 uses; Alpine 3.22.5 official rootfs boots; real
`git clone` over HTTPS works through the ZMUX shell executor). Android-side
gates (`nativeLibraryDir` exec, SELinux, ARMv7 ptrace performance) require
the on-device probe — run `gates` in the APK.

## 1. Why this works on modern Android (targetSdk 34, no root)

Android's W^X policy (since API 29) blocks `execve()` on anything inside the
app home directory:

- `untrusted_app` may not `execute_no_trans` on `app_data_file`
  (`system/sepolicy/private/untrusted_app_27.te` / `untrusted_app_all.te`).
- **But** the same policy explicitly keeps `mmap(PROT_EXEC)` of a file
  descriptor from the app home legal — `dlopen()` keeps working.

PRoot is a userland `chroot`/`execve` emulator that runs under `ptrace` and
starts guest processes through its **loader**, which is mapped into the tracee
with `PROT_EXEC`. No guest `execve` ever touches the blocked path. The only
real `exec()` in the chain is of the proot binary itself, and it lives in
`nativeLibraryDir` (`/data/app/.../lib/<abi>/`) — the read-only location
Google explicitly recommends for executables (`extractNativeLibs`).

The same approach is in production:

- **Kai 9000** (`SimonSchubert/Kai`) — targetSdk 37, proot + Alpine rootfs in
  app data, jniLibs packaging, `PROOT_LOADER` env. Apache-2.0.
- **UserLAnd / Andronix** — proot on unrooted modern Android for years.
- **Termux** stays on targetSdk 28 to keep this restriction off; ZMUX does
  not have to.

Packaging chain (verified against the pinned p4a commit):

1. `scripts/build_proot_android.py` cross-compiles proot/talloc with the NDK
   into `app/libs/<abi>/lib{proot,proot-loader,proot-loader32,talloc}.so`.
   **talloc gotcha (fixed 2026-07-31):** talloc's SONAME is `libtalloc.so.2`,
   and Android's linker resolves `DT_NEEDED` against *exact* filenames — so
   shipping the file as `libtalloc.so` made `libproot.so` die at exec time
   with `CANNOT LINK EXECUTABLE …: library "libtalloc.so.2" not found`.
   The build script now rewrites the NEEDED/SONAME strings in place to
   `libtalloc.so` using a strictly length-preserving substitution
   (`libtalloc.so\0\0`, 14 bytes — the naive `libtalloc.so\0` form is 13
   bytes and silently corrupts the whole ELF; see
   `docs/DEVICE_FAILURE_ANALYSIS.md` "Update 3"), enforces the size
   invariant in code, and then fails the build if any `DT_NEEDED` cannot be
   satisfied by the packaged files or if the talloc binding is wrong.
2. `buildozer.spec` → `android.add_libs_*` copies them into the p4a dist
   `libs/<abi>/` (verified in buildozer 1.5.0/1.6.0 `targets/android.py`).
3. p4a's `build.tmpl.gradle` packages `jniLibs.srcDir 'libs'` and — already
   by default — sets `packagingOptions { jniLibs { useLegacyPackaging = true } }`,
   so the `.so` files are **extracted** to `nativeLibraryDir` (exec-able).
4. `zmux/linuxenv.py` locates that directory via
   `PythonActivity.getApplicationInfo().nativeLibraryDir` (pyjnius, primed at
   startup — see `zmux/javabridge.py`) and execs `libproot.so` with
   `PROOT_LOADER=…/libproot-loader.so`. As a belt-and-braces for older APKs
   that shipped `libtalloc.so` under the wrong name, `proot_env()` mirrors it
   to a writable runtime dir as `libtalloc.so.2` and prepends that dir to
   `LD_LIBRARY_PATH`.

## 2. What the user gets

```
linux-setup        install Alpine 3.22.5 (~4 MiB download, SHA-512 verified)
git <args...>      REAL git: clone / branch / checkout / push, normal syntax
linux <cmd...>     any shell command: apk add ..., python3, sh, ...
alpine <cmd...>    alias of linux
gates              strict on-device acceptance probe (G1–G5)
```

- `git clone https://github.com/…` runs the real git binary inside Alpine.
  Credentials: HTTPS PAT via `~/.gitconfig` (in `~/` which is bound to `/root`),
  or SSH keys + `linux apk add openssh-client`.
- The whole ZMUX home is bound to `/root` inside the sandbox, so `cd`-ed
  projects are visible as `~/…`.
- No root. No SELinux changes. No `/system` writes. The sandbox lives in
  `APP_DIR/linux/`.

## 3. Pinned versions & provenance

| Component | Pin | Source | Verify |
|---|---|---|---|
| Alpine rootfs | `3.22.5` | `dl-cdn.alpinelinux.org` (docker-alpine v3.22 branch) | SHA-512 hardcoded from official `checksums.sha512` |
| PRoot | `4dba3af` (termux/proot) | codeload.github.com | built by CI |
| talloc | `2.4.2` (deepin mirror) | codeload.github.com | built by CI |

Alpine **3.23+ is excluded**: apk-tools 3 calls `execveat()`, which proot
cannot translate (termux/proot-distro#532/#595; Kai pins 3.22.5 for the same
reason).

## 4. Licenses

- PRoot: **GPL-2.0+** (STMicroelectronics). talloc: **LGPL-3.0**.
- ZMUX is AGPL-3.0. Exec'ing a separate GPL binary is distribution-compatible;
  the binaries are built from pinned public sources by our own CI, and the
  build script documents provenance. `scripts/build_proot_android.py` is
  adapted from Kai 9000's `build-proot.sh` (Apache-2.0, attributed).

## 5. On-device acceptance (the gates)

`gates` runs five unmocked checks and prints `[PASS]/[FAIL]`:

| Gate | Checks | Blocks |
|---|---|---|
| G1 `ptmx` | `/dev/ptmx` openable | PTY/TUI support (vim/htop/less) |
| G2 `proot-exec` | `libproot.so` execs from nativeLibraryDir | the whole W^X approach |
| G3 `alpine-boot` | Alpine boots, prints version | rootfs integrity |
| G4 `git-clone` | real `git clone` over HTTPS | GitHub integration |
| G5 `apk` | apk-tools runs in guest | package installation |

`gates` exits 0 only when every gate passes. On the sandbox (x86_64 Linux,
egress allowlist) G1/G2/G3/G5 pass and G4 fails only because `apk add git`
cannot reach Alpine's mirrors — on a phone with normal network it installs.

## 6. Known limits (honest)

- **No PTY yet.** If G1 fails on a device, vim/htop stay out of reach and
  interactive `git push` password prompts need a PAT. If G1 passes, a PTY
  engine can be layered on later.
- **ptrace overhead.** proot slows syscalls (roughly 2–5×); fine for
  `git`/`apk`/`sh`, painful for heavy compiles. ARMv7 low-end is the worst
  case and must be measured on the Infinix Smart 9 HD (G2/G3 latency).
- **DNS**: bootstrap writes `nameserver 8.8.8.8` + `1.1.1.1` fallback unless
  the host resolv.conf is usable; edit `linux/rootfs/etc/resolv.conf` to
  override.
- **`git` must be installed once**: `linux apk add git openssh-client`
  (needs network; mirrors are the official Alpine CDN).
- **32-bit ARM**: proot's 32-bit loader is built (`libproot-loader32.so`);
  Kai ships the same set, but ARMv7 remains the least-tested path.

## 7. Build pipeline

- `build-zmux-apk.yml` downloads the NDK (r28c, cached), runs
  `scripts/build_proot_android.py --out app/libs`, then `buildozer android debug`.
- Local builds without `app/libs/` still work — empty `add_libs` globs are
  skipped; `gates` then honestly reports G2 as missing.
- `validate.yml` at least smoke-checks the build script (`--help`).
