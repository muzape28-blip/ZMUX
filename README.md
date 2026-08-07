# ZMUX

**ZMUX** (*ZMUX Ember*) is an Android terminal app with a native Kotlin UI that
runs a **real Alpine Linux userland** on top of a Python-based PTY engine
(`realpty.py` + PRoot/Alpine). No root required.

It descends from the WebView terminal in [`ZABAWHEELS`](https://github.com/muzape28-blip/ZABAWHEELS):
same PTY / proot / Alpine engine, but the UI is now native Kotlin using Termux's
`terminal-view` + `terminal-emulator` (Apache-2.0) instead of a WebView.

> **Honest status:** this is a working PoC of the native Kotlin UI. Unit tests
> pass (see below), but it has **not** been validated as a release build on a
> physical device yet — the seccomp/PTY behaviour and IME resize behaviour still
> need on-device testing before a release.

---

## What it is now

- **Native Kotlin UI** (`experimental/zmux-kotlin/`) — true-black AMOLED field,
  ember/teal duotone, tabs, virtual keys. Uses Termux `terminal-view` +
  `terminal-emulator` as the ANSI parser / screen buffer.
- **Real PTY shell** — `proot → /bin/sh -l` inside a verified Alpine rootfs.
  The kernel does line editing, job control and `Ctrl+C`; this is a byte pump,
  not a fake pipe shell.
- **Simple prompt `Z$ `** — uncoloured, no invisible-width escape bytes. The
  directory basename appears only after an explicit `cd` (a `cd()` override),
  which eliminates the long-command wrap bug class entirely.
- **Performance-aware seccomp** — PRoot's seccomp accelerator is kept ON when a
  canary probe proves it works on the device, and only falls back to slow
  pure-ptrace (`PROOT_NO_SECCOMP=1`) when the kernel needs it (Android 14/15
  SIGSYS). This is what makes `apk add <pkg>` fast again.
- **No host config leaks** — DNS is written into the guest's own
  `/etc/resolv.conf` (public resolvers), never bound from the host. Binds are
  kept minimal (`/dev /proc /sys` + the user's own home dir).

---

## Repository layout

```
docs/
  decisions/   ADRs — why Kotlin for the UI and why not Rust
  progress/    PoC progress + WebSocket protocol spec
  analysis/    technical cross-checks (performance, etc.)
experimental/zmux-kotlin/
  app/src/main/python/zmux/   Python engine (realpty, linuxenv, python_shell, …)
  app/src/main/java/          Kotlin UI + Termux terminal wiring
  tools/                      protocol_check, mock WS server
  tests/                      pytest suite
Makefile                      standard dev commands
```

### Documentation

- [`docs/decisions/KOTLIN_RUST_DECISION.md`](docs/decisions/KOTLIN_RUST_DECISION.md)
  — architecture decision: Kotlin for the UI layer, using Termux
  `terminal-emulator` (Apache-2.0) as the ANSI parser / screen buffer, keeping
  the Python AGPL-3.0 engine (`realpty.py`, `zpip`, PRoot/Alpine), and **not**
  adopting Rust.
- [`docs/progress/PROGRESS_KOTLIN_UI_POC.md`](docs/progress/PROGRESS_KOTLIN_UI_POC.md)
  — progress report, architecture, and the WebSocket protocol spec.
- [`docs/analysis/CROSSCHECK_PERFORMANCE.md`](docs/analysis/CROSSCHECK_PERFORMANCE.md)
  — performance cross-check (`apk add` vs Termux + proot-distro) and the
  implementation status of the three fixes (seccomp, keyboard resize, prompt).

---

## Quickstart & local development

The root [`Makefile`](Makefile) mirrors the CI steps:

```bash
# 1. Run RFC-6455 protocol conformance + rootfs-installer + W^X + APP_DIR checks
make test

# 2. Build the Android Debug APK in experimental/zmux-kotlin/ (needs JDK 17 + Android SDK)
make build

# 3. Run Android Lint
make lint

# 4. Clean build outputs
make clean
```

`make test` currently runs:
- **protocol-check** — RFC-6455 conformance (9/9 gates): `?token=` auth, `action`
  control, `TIOCSWINSZ` resize, `Ctrl+C` (`0x03`) interrupt.
- **linuxenv-test** — Linux rootfs installer regression.
- **wx-safety-test** — W^X / "Permission denied" safety gates.
- **app-dir-test** — APP_DIR alignment gates (Chaquopy AssetFinder / rootfs location).

### Manual protocol check (without Make)

```bash
python3 experimental/zmux-kotlin/tools/mock_pty_ws_server.py --port 8011 --token dev &
python3 experimental/zmux-kotlin/tools/protocol_check.py --port 8011 --token dev
```

---

## CI / CD

Workflows live in [`ci/workflows/`](ci/workflows/) (and are mirrored to
`.github/workflows/` where the GitHub App has `workflows` permission):

- **`ci.yml`** — runs on `push`, `pull_request`, and `workflow_dispatch`:
  - `protocol-check` — RFC-6455 conformance (9/9 gates).
  - `build-android-apk` — Gradle 8.7 / JDK 17 `:app:assembleDebug`, uploads the
    `zmux-kotlin-debug-apk` artifact, and runs `:app:lintDebug` (uploads
    `zmux-kotlin-lint-report`).
  - `ci-summary` — build status + artifact list in the GitHub Actions summary.
- **`release.yml`** — builds and uploads a release APK (`zmux-kotlin-release-apk`)
  when a `v*` tag is pushed.

> **Note:** the GitHub App connection in Arena currently lacks the `workflows`
> permission, which is why workflow files are also kept under `ci/workflows/`.
> To activate CI on GitHub, copy `ci/workflows/` to `.github/workflows/` (or
> reconnect the GitHub App with `workflows` permission enabled).

---

## Development history & engineering notes (archive)

These sections record the problems that shaped the current design. They are
kept as a trail (honesty principle), not as the current "about" text.

### Android W^X ("Permission denied") + auto-reopen Alpine

Old symptom when reopening the app:

```
: /data/user/0/com.zmux.terminal.debug/files/.zmuxrc[1]:
  /data/user/0/com.zmux.terminal.debug/files/bin/clear: Permission denied
```

**Root cause:** the APK targets SDK 34, and since Android 10 (target SDK 29+)
the kernel/SELinux blocks `execve()` of every regular file in the app-private
directory (`/data/user/0/<pkg>/files/...`) — the **W^X** rule. `chmod 755` does
not help; `execve` always returns `EACCES`. The Python backend wrote CLI
wrappers under `files/bin`, and the bootstrap shell had `files/bin` first on
`PATH`, so `clear` in `.zmuxrc` hit that wrapper.

**Fix (all paths):**
1. **Kotlin bootstrap shell** — `PATH` is now system-only
   (`/system/bin:/system/xbin:/vendor/bin`); `files/bin` never appears on
   `PATH`. `linux-setup` still runs via `sh <path>` (interpreter only needs to
   *read* the file).
2. **Auto-reopen Alpine** — if a verified rootfs exists (`bin/sh` with busybox
   absolute symlinks resolved, `etc/.zmux-rootfs` marker, executable
   `libproot.so`), `createNewSession()` opens `proot → guest /bin/sh -l`
   directly, so the user is no longer stranded in the bootstrap shell after
   close/reopen.
3. **Kotlin proot launcher matches Python** — `libtalloc` SONAME self-heal,
   storage binds only for actually readable+executable paths, guest
   mountpoints auto-created.
4. **Python runtime** — `zmux.paths.android_exec_blocked()` detects the W^X
   sandbox: wrappers are still written + `chmod 0755` (for interpreter/desktop
   use), but `BIN_DIR` is no longer prepended to `PATH`, and stale app-private
   `PATH` entries are scrubbed on import.

**Test gates:** `tests/test_wx_permission_safety.py` (15 gates), including
executing the actual `.zmuxrc` bytes extracted from the Java source under a
trap-filled `bin` dir (negative control proves the old layout hits the trap).

### "Rootfs path disappeared" (APP_DIR Chaquopy vs filesDir)

Old symptom:

```
[Chaquopy Error] Rootfs path disappeared before PRoot launch:
/data/user/0/com.zmux.terminal.debug/files/linux/rootfs
```

**Root cause:** Chaquopy is not python-for-android; it does not set
`ANDROID_PRIVATE`/`ANDROID_ARGUMENT`/`ANDROID_APP_PATH`, and it extracts app
Python sources to `<filesDir>/chaquopy/AssetFinder/app`. `resolve_app_dir()`
fell back to the `__file__`-based path, so the rootfs was installed under the
AssetFinder tree while Kotlin looked under `filesDir`.

**Fix:**
1. **APP_DIR contract (Kotlin)** — `ZmuxTerminalActivity.onCreate` exports
   `Os.setenv("ANDROID_PRIVATE", filesDir, true)` **before** `Python.start()`
   (APP_DIR is resolved at import time).
2. **No guessed paths in Kotlin** — `launchLinuxSession()` /
   `detectInstalledLinux()` ask `zmux.linuxenv` for the real `rootfs_dir()` /
   `home_dir()`.
3. **Python safety net** — `resolve_app_dir()` refuses the Chaquopy asset tree
   as the runtime root; without a host export it falls back to `HOME`
   (= `filesDir` under Chaquopy).
4. **Legacy install migration** — `linuxenv.migrate_legacy_install()` adopts a
   stranded rootfs via rename (O(1), never copies on the UI thread), merges old
   `home` contents without overwriting, and reports what it deliberately leaves
   alone.

**Test gates:** `tests/test_app_dir_alignment.py` (23 gates), also auto-run from
`tests/test_wx_permission_safety.py` via `load_tests`.

---

## License

Engine is AGPL-3.0. Termux is used as Apache-2.0 Maven artifacts
(`terminal-view`, `terminal-emulator`), not as a copy of the GPLv3 app.
