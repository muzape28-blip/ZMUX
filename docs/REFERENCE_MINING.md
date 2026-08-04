# Harta Karun — Reference Mining Report

**Status:** ✅ Waves A, B, C, D implemented (2026-07-31). See
[IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) for what shipped, what
changed versus this plan, and what is still open.
**Date:** 2026-07-31
**References mined:**

| # | Repo | License | What it really is |
|---|------|---------|-------------------|
| 1 | [Lerist/android-shell](https://github.com/Lerist/android-shell) | Apache-2.0 | Fork of Chainfire `libsuperuser`. A *shell command runner* library (Java), not a terminal. |
| 2 | [RohitKushvaha/ReTerminal](https://github.com/RohitKushvaha01/ReTerminal) | MIT (but embeds Termux `terminal-view`) | Material3 Compose UI wrapped around Termux's terminal engine + Alpine/proot rootfs. |
| 3 | [pavelc4/Rin](https://github.com/pavelc4/Rin) | MIT | Kotlin/Compose UI + **Rust** terminal engine via JNI + `rpkg`, a pacman-style client for the Termux apt repo. |
| 4 | [termux/termux-app](https://github.com/termux/termux-app) | **GPLv3** (except `terminal-emulator`/`terminal-view` = **Apache-2.0**) | The reference implementation. Everyone else copies it. |

> **Licensing note up front:** ZABAWHEELS is **AGPL-3.0**. GPLv3 code from `termux-app/app/` is
> *not* AGPL-compatible for direct copying without care, but the two modules we actually want
> ideas from — `terminal-emulator` and `terminal-view` — are **Apache-2.0**, which is
> AGPL-compatible. android-shell is Apache-2.0, ReTerminal and Rin are MIT. **All the patterns
> recommended below are re-implementations of ideas, not code copies** — but the licence
> position is clean either way.

---

## 0. The honest baseline: what ZMUX actually is today

Before the treasure list, one finding has to come first because it changes how you read
everything else.

**ZMUX has no PTY.** `app/zmux/pty_session.py` is named `PTYTerminalSession` and the docs
describe "POSIX PTY sessions (`os.openpty`) with automatic fallback to standard pipes", but:

```
$ grep -rn "openpty\|import pty\|ptmx" app/zmux/
app/zmux/pty_session.py:160:  # No child PTY needs ioctl(TIOCSWINSZ); we just track the width...
```

There is no `openpty`, no `/dev/ptmx`, no fallback path, no `termios`. The module's own
docstring is honest about it ("There is intentionally no PTY and no `/system/bin/sh` child
here"), but `README.md:29`, `ROADMAP_STATUS.md:21,103` and `REFACTOR_REPORT.md:68` all still
claim `os.openpty` is implemented with SELinux fallback. **That's the single biggest
integrity gap in the repo** — a project whose selling point is "honest, transparent"
documentation is currently over-claiming its core engine.

What ZMUX *is*: a Python-native line interpreter with a shell persona. That's a legitimate
and in some ways smarter design for a p4a app (see §1.1), but it must be described accurately.

**Recommendation R0 (do this regardless of everything else):** fix the four doc locations to
describe the real architecture — "virtual terminal backed by the embedded CPython runtime,
no Unix PTY." Cheap, zero risk, and it protects the project's main differentiator.

---

## 1. The genuine treasure (ranked by value/effort)

### T1 — Streaming output. The single highest-value find. 🥇

**Where it comes from:** Termux `TerminalSession.java:133-148` (reader thread → `ByteQueue` →
UI), Rin `session.rs:38-90` (16 KiB read loop → engine), android-shell `StreamGobbler.java`
(a thread whose entire purpose is "read the stream *now* or the child deadlocks").

All three references, built by different people in different languages, independently arrived
at the same shape: **a dedicated reader thread that forwards bytes to the display the instant
they arrive.** android-shell even documents *why* in its constructor javadoc: not draining
promptly pauses the native process or deadlocks `waitFor()`.

**ZMUX does the opposite.** `python_shell.py:316-341` captures the whole command into
`io.StringIO` via `contextlib.redirect_stdout`, and `pty_session.py:384-397` emits it only
after `execute()` returns:

```python
result = self.shell.execute(source, force_python=force_python)   # ← blocks to completion
output = (result["stdout"] + result["stderr"]).replace("\n", "\r\n")
if output: self._emit(...)                                        # ← everything at once
```

Verified on this machine:

```
$ python3 /tmp/t.py     # for i in range(3): print('tick', i); time.sleep(0.4)
RETURNED_AFTER_SEC: 1.2
STDOUT: 'tick 0\ntick 1\ntick 2\n'
```

The user stares at a frozen terminal for 1.2 s, then all three ticks land simultaneously.
Scale that to `zpip install`, a `for` loop over files, or any long job and the app looks hung.
It also affects subprocesses: `_exec_subprocess_inner` uses `communicate()`, so `ping`, `logcat`
or any streaming Android binary produces **nothing until it exits**.

Worse, and this is the part that actually breaks correctness rather than feel — **`input()`
prompts are invisible**:

```
$ python3 /tmp/t3.py
RETURNED AFTER: 0.3
STDOUT SEEN BY TERMINAL ONLY AT END: 'What is your name? Hello Zaba\n'
```

`input("What is your name? ")` writes the prompt to the captured buffer, blocks for stdin, and
the user is asked to answer a question **they cannot see**. The interactive stdin feature
shipped in the last release is effectively unusable for any prompt-driven script.

**The fix is small and very ZMUX-shaped.** Don't rebuild the engine — replace the
`StringIO` sink with a line-buffered writer that calls `self._emit()` directly:

```python
class _StreamingOut(io.TextIOBase):
    """stdout/stderr proxy that pushes to the websocket as data is written."""
    def __init__(self, emit): self._emit, self._buf = emit, ""
    def write(self, s):
        self._buf += s
        while "\n" in self._buf:                    # flush complete lines
            line, self._buf = self._buf.split("\n", 1)
            self._emit((line + "\r\n").encode("utf-8", "replace"))
        if len(self._buf) > 512: self.flush()       # and long partial lines
        return len(s)
    def flush(self):
        if self._buf: self._emit(self._buf.encode("utf-8", "replace")); self._buf = ""
    def isatty(self): return True
```

Then `_exec_python` redirects to that instead of `StringIO`, and `_QueueInput.readline`
flushes before blocking so the prompt appears. For subprocesses, swap `communicate()` for the
same reader-thread pattern all three references use. Keep returning the dict for the REST API
by teeing into a buffer — that preserves `test_terminal.py` / `test_server.py` contracts.

> **Bonus:** flushing partial lines >512 B also makes `print(..., end="")` progress bars and
> `\r`-based spinners work, which is what makes `rpkg`'s download bar (below) possible at all.

---

### T2 — Multiple sessions 🥈

Every single reference has this; ZMUX has a hard-coded singleton
(`pty_session.py:418-425`, `_pty_session` global).

- **ReTerminal:** `SessionService.kt` — `hashMapOf<String, TerminalSession>()` + a bound
  `Service`, `createSession(id)/getSession(id)/terminateSession(id)`, auto-`stopSelf()` when
  the last one closes.
- **Rin:** `SessionManager.kt` — same idea, plus `renameSession` and index clamping on close
  (`activeIndexState.intValue.coerceIn(0, sessions.size - 1)`) — a nice detail that avoids the
  classic "closed the last tab and crashed" bug.
- **ReTerminal** also ships keyboard shortcuts for it (`KeyShortcutHandler.kt`: new / close /
  prev / next session).

For ZMUX this is genuinely cheap because the session object is already self-contained. Turn
the global into a registry keyed by id, let the websocket carry
`{"action":"session","id":"2"}`, and give each session its own `PythonShell` (which already
holds its own `cwd` and `globals`). The UI cost is one tab strip. This is the #1 *feature*
request-shaped gap versus every competitor — v1.1.0 already lists it, and it's more tractable
than the roadmap implies.

**Design note worth stealing from ReTerminal:** sessions live in a *foreground Service* with an
ongoing notification and an EXIT action. On Android 12+ that is the difference between "my
long build survived me switching apps" and the OS silently killing it. Rin does the same
(`TerminalSessionService.kt`, `foregroundServiceType="specialUse"`). ZMUX runs everything
inside the p4a activity process — a backgrounded ZMUX is a dead ZMUX.

---

### T3 — The virtual-keys bar, done properly 🥉

ZMUX's toolbar (`terminal.html:168-182`) is 13 hardcoded `<button onclick="sendRaw(...)">`.
It has no Ctrl, no Alt, and no way to type `Ctrl+R`, `Ctrl+Z`, `Ctrl+L`, `Alt+.` — a real
gap for a terminal.

**Termux's design** (via ReTerminal's vendored `VirtualKeysConstants.java` /
`VirtualKeysInfo.java`) is worth copying conceptually because it is *data-driven*:

- The whole keyboard is a **JSON array of arrays** — one inner array per row.
- Each key is `'ESC'` or `{key:'HOME', popup:'END', display:'⌂'}`.
- `macro: "CTRL f d"` sends a *sequence*.
- `popup:` = swipe-up alternate key — doubles key density without doubling screen space.
- Aliases (`CONTROL`→`CTRL`, `PAGE_UP`→`PGUP`) so config is forgiving.
- `PRIMARY_REPETITIVE_KEYS = [UP, DOWN, LEFT, RIGHT, BKSP, DEL]` — auto-repeat on hold.

**Rin's `ExtraKeysBar.kt`** is the trimmed-down version and shows the two behaviours that
matter most in practice: **sticky modifiers** (`ctrlActive` state — tap CTRL, then tap `c`)
and **hold-to-repeat** (`REPEAT_INITIAL_DELAY_MS = 400`, `REPEAT_INTERVAL_MS = 50`).

For ZMUX the sticky-Ctrl part is ~20 lines of JS: when `ctrlActive` and the next key is a
letter, send `String.fromCharCode(c.toUpperCase().charCodeAt(0) - 64)`. The JSON layout can be
a `zmux.json` preference later. High user-visible value, low risk, pure frontend.

---

### T4 — Package manager UX from `rpkg`

ZMUX's `zpip` is, security-wise, **already better than `rpkg`** — worth stating plainly:

| | zpip | rpkg |
|---|---|---|
| Path traversal guard | `..`/absolute/empty rejected, resolved-path escape check (`zpip.py:193-228`) | **none found** — `strip_upstream` filters prefixes but never checks `..` |
| Hash verification | mandatory SHA-256, HTTPS-only | not in the install path |
| Transactional | backup + rollback + dep-unwind | writes as it goes, saves DB per package |
| Size limits | 100 MiB + 3× uncompressed | none |

So don't copy rpkg's *mechanics*. Copy its **UX**, which is much better than ZMUX's silent block:

1. **Plan-then-confirm.** `install.rs:52-70` prints `Packages (N) a-1.0 b-2.0`, total download
   size, total installed size, then `:: Proceed with installation? [Y/n]`. ZMUX resolves
   dependencies recursively (`zpip.py:379-410`) and just… does it. A dry-run summary is a
   small refactor: resolve first, print, confirm, then commit.
2. **Live download progress** (`install.rs:96-146`) — `\x1b[2K\r` + a 20-char bar + MiB + KiB/s,
   throttled to every 100 ms. ZMUX's `_download` (`zpip.py:165-190`) already loops in 64 KiB
   chunks with a running total and knows `Content-Length` — **the data is all there, it just
   isn't rendered.** ~15 lines. (Depends on T1 for partial-line flushing.)
3. **`explicit` vs dependency marking** (`install.rs:171`, `InstalledPackage.explicit`). ZMUX's
   DB records no such flag, so `zpip list` can't distinguish "I asked for this" from "pulled
   in as a dep", and there's no path to an `autoremove`. One boolean at install time.
4. **`[installed]` marker in search results** — trivial, `search()` already loads the DB.

---

### T5 — Environment correctness for child processes

`terminal.py:97-130` builds a careful env — `HOME`, `TERM=xterm-256color`, `LANG=C.UTF-8`,
`PATH` with `BIN_DIR` + Android system dirs, `PYTHONPATH` with `USER_PACKAGES_DIR`.

**It is never used.** `self._env` is assigned at `terminal.py:95` and referenced nowhere else,
and the only actual `Popen` in the codebase (`python_shell.py:431-433`) passes `cwd=` but **no
`env=`** — so children inherit the raw app environment.

Compare **ReTerminal's `MkSession.kt:57-84`**, which is the best checklist available of what an
Android child process actually needs:

```
PATH, HOME, TMPDIR, PREFIX, LD_LIBRARY_PATH, LINKER (linker64 vs linker),
NATIVE_LIB_DIR, TERM=xterm-256color, COLORTERM=truecolor, LANG=C.UTF-8,
+ passthrough: ANDROID_ART_ROOT, ANDROID_DATA, ANDROID_I18N_ROOT, ANDROID_ROOT,
  ANDROID_RUNTIME_ROOT, ANDROID_TZDATA_ROOT, BOOTCLASSPATH, DEX2OATBOOTCLASSPATH,
  EXTERNAL_STORAGE
```

Those `ANDROID_*` / `BOOTCLASSPATH` passthroughs are not decoration — Android system binaries
misbehave without them. **Two-line fix** (`env=self._env` on the `Popen`) plus adopting the
passthrough list. Also note ZMUX's `_find_executable` (`python_shell.py:367-372`) hardcodes six
directories and **never searches `BIN_DIR`**, so the `zpip`/`help`/`zmux-info` wrappers that
`paths.py` carefully generates are unreachable from the pipeline executor. It should honour
`PATH` instead.

---

### T6 — `~/.zmuxrc` startup file

Rin's `pty.rs:36-38` sets `ENV=$HOME/.mkshrc`; ReTerminal's `init.sh` sources `/etc/profile`
and sets `PS1`. Neither is complicated, both make the terminal feel like *the user's*.

ZMUX has a natural version: on session start, if `~/.zmuxrc` exists, feed it through the same
line-execution path before showing the first prompt. It composes perfectly with the existing
`seed_examples()` onboarding, and gives users aliases/imports/`PS1` without any new machinery.

---

### T7 — Crash visibility

`main.py:8-25` writes `zmux_crash.log` on a top-level exception, which is good — but a crash in
any *worker thread* is swallowed by the blanket `except Exception` in `_exec_loop`
(`pty_session.py:318-321`) and never reaches disk.

ReTerminal installs a `Thread.UncaughtExceptionHandler` globally (`CrashHandler.kt`). Python's
equivalent is `threading.excepthook`, set once in `main.py`, appending to the same crash log.
Roughly 10 lines, and it's the difference between "the app got weird" and an actionable trace
during the pending device-testing phase.

---

## 2. Deliberately NOT recommended

Being clear about what to *reject* is half the value of a reference hunt.

| Idea | Source | Why not |
|---|---|---|
| **Root / `su` support** | android-shell (whole library), Rin `RootHelper.kt` | ZMUX's README explicitly promises no root. android-shell is a `libsuperuser` fork — its entire value proposition is `Shell.SU.run()`. Off-mission. |
| **proot + Alpine rootfs** | ReTerminal (`init-host.sh`, 3 × `.tar.gz` in assets) | Ships tens of MB of rootfs, needs `libproot.so`/`libloader.so` prebuilts, and re-creates the whole "real Linux" surface ZMUX deliberately avoided. Contradicts "not claiming Termux-level access". |
| **Rewrite the engine in Rust/JNI** | Rin (`src/core`, `src/parser/ansi.rs`) | Rin's Rust core is genuinely nice, but ZMUX is a Buildozer/p4a Python app. Adding `cargo-ndk` + JNI would blow up the reproducible-build contract (pinned `p4a.commit`, `runtime-lock.json`) for no user-visible gain — xterm.js already does the ANSI parsing client-side. |
| **Copy Termux's `rpkg`/apt ecosystem** | Rin `rpkg` | Termux `.deb`s are built against Termux's prefix and linker; Rin has to **binary-patch ELF interpreters and rewrite `/data/data/com.termux/files` strings inside every file** (`extract.rs:38-100`, with a `const _: () = assert!` keeping the replacement byte-length identical). Ingenious, and exactly the kind of fragility ZABAWHEELS' curated-wheel model exists to avoid. |
| **ELF interpreter patching** | Rin `extract.rs:38-76` | Same reasoning. ZMUX ships wheels for its own runtime; no patching needed. |
| **`extractNativeLibs` binary trick** | Termux / general Android practice | Worth *knowing*: since targetSdk 29, `exec()` on app-home files is blocked (W^X), and the standard workaround is shipping binaries as `lib*.so` in `jniLibs` and running them from `nativeLibraryDir`. ZMUX sidesteps this entirely by never exec'ing its own binaries — a real architectural advantage. **But it means `paths.py`'s `#!/system/bin/sh` wrappers in `BIN_DIR` (app-private, chmod 0755) may simply refuse to execute on modern Android.** Flagging as a risk to verify on-device, not a change to make blind. |

---

## 3. Suggested sequencing

| Wave | Items | Effort | Risk |
|---|---|---|---|
| **A — truth & correctness** | R0 docs fix · T1 streaming output · T5 `env=` two-liner + `BIN_DIR` on PATH | S–M | low |
| **B — feels like a terminal** | T3 sticky-Ctrl virtual keys · T6 `~/.zmuxrc` · T7 `threading.excepthook` | S | low |
| **C — the headline feature** | T2 multi-session registry + tab strip | M–L | medium |
| **D — package UX** | T4 plan/confirm · progress bar · `explicit` flag · `[installed]` | M | low |

Wave A is the one I'd argue for first: T1 fixes a user-visible correctness bug (invisible
`input()` prompts), T5 is two lines, and R0 restores the documentation integrity the project
markets itself on. None of it needs new dependencies, new permissions, or any change to the
pinned build contract.

---

## 4. Open questions for you

1. **Multi-session** — real parallel sessions (each with its own `PythonShell` + worker
   thread), or just detachable tabs over one interpreter? Parallel is more useful and more work.
2. **Foreground Service** — worth adding `FOREGROUND_SERVICE` + `POST_NOTIFICATIONS` to keep
   sessions alive when backgrounded? It breaks the current "INTERNET only" permission boast,
   which is a genuine marketing asset. Trade-off, your call.
3. **Scope of T1** — Python-side streaming only (quick, fixes `input()`), or subprocess
   streaming too (bigger, fixes `ping`/`logcat`)?
4. **`BIN_DIR` wrappers** — do you have a device where you can confirm whether
   `/data/.../bin/zpip` actually executes on Android 10+? If it doesn't, that whole mechanism
   needs rethinking and it changes T5's shape.
