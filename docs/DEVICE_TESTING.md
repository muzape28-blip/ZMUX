# Device Testing

## Status

| Component | ARMv7 | ARM64 |
|-----------|-------|-------|
| APK Install | ✅ Verified | ⏳ Pending |
| Terminal UI | ✅ Verified | ⏳ Pending |
| Command Execution | ✅ Verified | ⏳ Pending |
| Python Runtime | ✅ Verified | ⏳ Pending |
| zpip Install | ⏳ Pending | ⏳ Pending |
| Native Smoke (gates) | ✅ 5/5 PASS | ⏳ Pending |

## First on-device results (2026-07-31, ARMv7)

The first real-device pass (Infinix Smart 9 HD class, `armeabi-v7a`) found
three failures that desktop CI could not see. Root causes and fixes are
documented in [DEVICE_FAILURE_ANALYSIS.md](DEVICE_FAILURE_ANALYSIS.md):

| Check | Result | Root cause → fix |
|---|---|---|
| `linux apk add …` (G5 / proot exec) | ❌ `CANNOT LINK EXECUTABLE …: library "libtalloc.so.2" not found` | talloc shipped as `libtalloc.so` while `libproot.so` needs `libtalloc.so.2` by exact filename → build script now rewrites NEEDED/SONAME in place + verifies all `DT_NEEDED` resolve; runtime self-heal for old APKs |
| `zmux-setup-storage` | ❌ `ClassNotFoundException: org.kivy.android.PythonActivity` (system class loader on worker thread) | `autoclass()` from the exec worker thread → primed Java bridge (`zmux/javabridge.py`) resolves app classes on the main thread at startup; storage requests permissions via the cached `mActivity` |
| `zpip install nano` then `nano` | ⚠️ install "succeeds", `nano` fails | PyPI `nano` is a Django library, not GNU nano; and TUI editors need a PTY ZMUX lacks → zpip warns on the name collision; the shell explains "needs a real TTY" instead of a `NameError` |

Re-verify after installing a rebuilt APK: `gates` (G2/G3/G5 must pass),
`zmux-info` ("Proot NEEDED" must read `libtalloc.so`, not `libtalloc.so.2`),
`linux apk add git openssh-client`, `zmux-setup-storage`. G2 now also reads
the shipped binary's `DT_NEEDED` on-device and flags stale binaries
explicitly (see `app/zmux/elfscan.py`).

## Test Checklist

### Basic Terminal
- [ ] APK installs and launches
- [ ] Terminal UI renders correctly
- [ ] `echo hello` produces output
- [ ] `python3 --version` works
- [ ] `python3 -c "print(42)"` works
- [ ] `cd` changes directory
- [ ] `pwd` shows correct path
- [ ] `clear` clears screen
- [ ] `help` shows help
- [ ] History navigation works (up/down arrows)
- [ ] Ctrl+C stops running process

### Python Execution
- [ ] Interactive Python REPL works
- [ ] `python3 script.py` runs scripts
- [ ] stdin input works
- [ ] Exit codes are accurate
- [ ] stdout/stderr separated

### Package Manager
- [ ] `zpip list` works
- [ ] `zpip search <name>` works
- [ ] `zpip info <name>` works
- [ ] `zpip install <pure-python-package>` works
- [ ] `zpip uninstall <package>` works
- [ ] `zpip verify <package>` works
- [ ] `zpip doctor` shows runtime info

### Security
- [ ] Path traversal blocked for built-in cd
- [ ] HTTPS-only downloads
- [ ] SHA-256 verified on install
- [ ] Auth token required for API

### Alpine Sandbox (PRoot) — run `gates` in the APK
- [ ] G1 `ptmx`: `/dev/ptmx` openable (PTY possible?)
- [ ] G2 `proot-exec`: `libproot.so` execs from `nativeLibraryDir` (W^X gate)
- [ ] G3 `alpine-boot`: Alpine 3.22.5 boots inside proot
- [ ] G4 `git-clone`: real `git clone` over HTTPS works
- [ ] G5 `apk`: apk-tools runs in the guest
- [ ] `linux-setup` downloads rootfs, verifies SHA-512, extracts atomically
- [ ] `git clone` → `git branch` → `git checkout` normal workflow
- [ ] `linux apk add <pkg>` installs inside the sandbox
- [ ] Ctrl+C interrupts a long `git clone` / `linux` job without freezing
- [ ] 8 sessions each running `git`/`linux` do not corrupt each other

### Performance
- [ ] Terminal responsive on Android Go devices
- [ ] No excessive memory usage
- [ ] No battery drain during idle

## Reporting Results

Use the [Device Test Issue Template](../.github/ISSUE_TEMPLATE/device-test.yml) to report test results.

## Legend

- ✅ Verified — tested on real device
- ⏳ Pending — not yet tested
- ❌ Failed — issue found
- ⚠️ Partial — works with limitations
