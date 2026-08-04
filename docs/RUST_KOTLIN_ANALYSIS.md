# Should ZMUX adopt Rust and Kotlin?

**Date:** 2026-07-31
**Question:** would rewriting ZMUX with Rust and Kotlin (like Rin) be a positive
step, or a move away from what ZMUX is trying to be?
**Method:** the load-bearing assumption was tested, not assumed.

---

## Short answer

**Negative for ZMUX — but not for the reason you would expect.**

Rust and Kotlin are not bad technologies here; Rin proves they work. The problem
is that **the main thing they would buy you, you can already have for free.**

I tested it. **CPython's standard library already contains everything needed to
build a full PTY terminal** — the exact capability ZMUX is missing today, and
the exact capability people assume requires native code.

---

## 1. The experiment that decides this

The usual argument for Rust/JNI is: *"a real PTY needs native code, so we must
go native."* I tested that claim directly with ~40 lines of pure Python:

```python
master, slave = os.openpty()                      # real PTY pair
fcntl.ioctl(master, termios.TIOCSWINSZ, ...)      # real window size
subprocess.Popen(["/bin/sh","-i"], stdin=slave, stdout=slave, stderr=slave,
                 start_new_session=True)          # real shell on the PTY
```

Result:

```
os.openpty() -> master fd=5 slave fd=6
slave device : /dev/pts/0
TIOCSWINSZ ioctl(24x80): OK

=== Does the shell think it has a TTY? ===
    /dev/pts/0
    STDIN IS A TTY                    ← a real TTY

=== Do SHELL FEATURES work? ===
    CHAIN_OK                          ← && works
    subst                             ← $(...) works

=== Does a TUI program run? ===
    top - 12:42:35 up 48 min, load average: 0.00, 0.01, 0.00
    Tasks:  77 total,   1 running,  76 sleeping
```

**`top` rendered.** That is the single headline feature ZMUX lacks, achieved in
stdlib Python, no Rust, no Kotlin, no JNI, no NDK.

### Why Termux needed C and ZMUX does not

Termux's `termux.c` exists for a language limitation, not an Android one:

```c
int ptm = open("/dev/ptmx", O_RDWR | O_CLOEXEC);   // termux.c:36
```

Java and Kotlin have **no `fork()`, no `openpty()`, no `execvp()`**. JNI is their
*only* path to a PTY. Python has all four in the standard library:

```
os.forkpty  -> True      pty.spawn  -> True
os.execvp   -> True      os.openpty -> True
```

And the modules are not optional add-ons — `fcntl` and `select` are compiled
into libpython itself; `termios` is a standard C extension p4a always builds.

**Termux is also the proof that `/dev/ptmx` is reachable from an unprivileged
app**: it is a normal APK, no root, working on essentially every Android device.
The capability is not the blocker. (Some hardened Android Go builds do restrict
it — that needs on-device verification — but the platform permits it.)

---

## 2. What a PTY would do to the four known bugs

I ran the capability report's four bugs against a real PTY + real shell:

| Bug | Under a real PTY |
|---|---|
| `&&` silently ignored | **gone** — `built.txt` created |
| `2>&1` creates a file named `&1` | **gone** — no junk file |
| `cd` ignored by Python | **gone** — Python wrote into the `cd`'d directory |
| Typo → Python `SyntaxError` | **gone** — `command not found` |
| `$VAR`, globs, `~` | **work** |

> A real PTY does not *fix* these bugs. It **deletes the code that has them.**

`python_shell.py`'s hand-written pipeline parser, redirection handler, `cwd`
tracker and builtin `ls`/`cp`/`mv` — roughly 500 lines re-implementing a shell,
badly — all become unnecessary, because `/system/bin/sh` already does it
correctly. **The fix list from the capability report collapses into one task.**

---

## 3. What Rust and Kotlin would actually cost

### Cost 1 — the reproducible build contract, which is ZABAWHEELS' whole point

`toolchain/runtime-lock.json` pins CPython 3.14.2, `p4a_commit`, NDK 28c, clang
19, Cython, setuptools. `zpip` refuses wheels that do not match `runtime_id`
`zmux-py314-api26-p4a5c192d7b7308-r1`.

Adding Rust means pinning a Rust toolchain, `cargo-ndk`, a crate lockfile and
per-ABI `.so` artifacts **into that same contract**. Adding Kotlin means Gradle,
AGP, the Kotlin compiler and the Compose compiler. The verification story — the
thing that differentiates this project from every other Android terminal — gets
several times harder to keep honest.

### Cost 2 — it is a rewrite, not an addition

| Project | Lines |
|---|---|
| ZMUX today (entire app) | **3,980** |
| Rin (Rust engine + Kotlin UI) | 5,558 **+ 1,374** (rpkg) |
| Termux (`terminal-emulator` + `terminal-view` + JNI) | 11,363 |
| ReTerminal (Kotlin UI over Termux's engine) | 8,161 |
| **Pure-Python PTY session** | **~150–200** |

Rin's Rust core is 5,558 lines that reimplement an ANSI parser, grid, cell
buffer, cursor and scrollback. **ZMUX does not need any of that: xterm.js
already does the terminal emulation client-side.** Rin needs it because it
renders to a native Compose surface. ZMUX renders in a WebView. Adopting Rin's
architecture means writing thousands of lines to replace something you get free
from a library you already ship.

### Cost 3 — losing the Python-native advantage

ZMUX's genuine differentiator is that the *terminal and the language are the
same runtime*: `zpip` installs into the interpreter that the REPL is using, and
`zmux-info` can introspect it. Move the terminal into Rust and Python becomes
just another subprocess — at which point ZMUX is a worse Termux with a curated
wheel index bolted on.

### Cost 4 — build time and contributor reach

CI is already a 120-minute job. Adding `cargo-ndk` cross-compilation for two
ABIs plus a Gradle/Kotlin build extends that materially. And the contributor
pool changes from "knows Python" to "knows Python **and** Rust **and** Kotlin
**and** JNI **and** the NDK".

---

## 4. Where Rust/Kotlin *would* genuinely earn their place

To be fair to the idea, these are real and I would not dismiss them:

| Goal | Needs native? | Verdict |
|---|---|---|
| Native Compose UI instead of WebView | **Yes, Kotlin** | Legitimate — but it is a different product |
| Sessions surviving backgrounding | **Kotlin** (foreground Service) | ⚠️ p4a supports `services=...:foreground:sticky` in Python; Kotlin not required |
| Rendering 100k lines of scrollback at 60fps | Rust helps | Not a real ZMUX workload |
| Termux `.deb` compatibility (`rpkg`) | Rust convenient | Requires ELF interpreter patching — explicitly rejected in the reference mining |
| CPU-bound work (compression, hashing) | Rust helps | Already handled by CPython C extensions |
| A PTY | **No** | stdlib does it — proven above |

The honest one is the **UI**: Jetpack Compose would be smoother and more native
than a WebView, and would remove the loopback-server/token/CSP machinery. But
that is *rewriting ZMUX as a different app*, not improving this one. It also
throws away xterm.js, which is currently doing all the terminal emulation for
free.

---

## 5. Recommendation

**Do not adopt Rust or Kotlin. Adopt the PTY instead.**

The measured comparison:

| | Pure-Python PTY | Rust + Kotlin rewrite |
|---|---|---|
| Gets a real TTY, TUI apps, full shell | ✅ | ✅ |
| Fixes all 4 known bugs | ✅ (deletes them) | ✅ |
| New code | **~150–200 lines** | ~5,000–10,000 lines |
| Code *removed* | **~500 lines** of fake shell | — |
| New toolchains | **none** | Rust, cargo-ndk, Gradle, Kotlin, NDK |
| Build contract impact | **none** | significant |
| Keeps Python-native `zpip` integration | ✅ | ✗ weakened |
| Time to working | days | months |

### Suggested direction

1. **Build a real PTY session in Python** (`os.openpty` + `/system/bin/sh` +
   a reader thread → the existing websocket). xterm.js is *already* a full
   terminal emulator, so it will simply start working properly.
2. **Keep the Python REPL as a mode**, not as the whole terminal. That
   preserves the differentiator (`zpip`, `zmux-info`, embedded runtime) while
   the shell becomes a real shell.
3. **Delete the hand-written shell emulation** once the PTY works — pipeline
   parser, redirection, `cwd` tracking, builtin coreutils. That is where the
   bugs live.
4. **Verify `/dev/ptmx` on a real device first.** This is the one genuine risk:
   Termux proves it works broadly, but the Infinix Smart 9 HD target is Android
   Go, and if SELinux denies it there, ZMUX needs the current engine as a
   documented fallback. **This single test should gate the whole plan.**

Rust and Kotlin are the right answer to *"how do we build a terminal in a
language without `fork()`"*. ZMUX is written in a language that has `fork()`.
It should use it.

> **In one line:** Rust/Kotlin would cost a rewrite, two toolchains and the
> build contract, to buy something 200 lines of stdlib Python already provides.
