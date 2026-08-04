# Implementation Notes — Reference Mining Waves A–D

**Date:** 2026-07-31
**Plan:** [REFERENCE_MINING.md](REFERENCE_MINING.md)
**Tests:** 190 app + 32 infra passing (was 134 + 32)

This records what was actually built, what it cost, and — most importantly —
what claims changed. The project's stated value is honest documentation, so
where a promise was narrowed or a boast dropped, it is written down here
rather than quietly edited away.

---

## 1. The documentation correction (the uncomfortable one)

`README.md`, `ROADMAP_STATUS.md` and `REFACTOR_REPORT.md` claimed ZMUX ran
**"POSIX PTY sessions (`os.openpty`) with automatic fallback to standard pipes
when SELinux denies `/dev/ptmx`"**. Verified against the code:

```
$ grep -rn "openpty\|import pty\|ptmx" app/zmux/
app/zmux/pty_session.py:160:  # No child PTY needs ioctl(TIOCSWINSZ)...
```

No `openpty`, no `/dev/ptmx`, no fallback path, no `termios`. The module's own
docstring was accurate ("intentionally no PTY"); the marketing docs were not.

**What changed:** a new *Terminal model* section in the README states plainly
that ZMUX is a virtual terminal, and lists the cost — **no TUI programs (`vim`,
`htop`, `less`), no job control, no login shell**. The inaccurate historical
changelog entry in `ROADMAP_STATUS.md` was *annotated, not rewritten*, so the
record of the mistake survives.

**Claim dropped:** "Not a simulated terminal — commands execute via real
subprocesses and pseudo-terminals (PTY)". Replaced with two narrower claims
that are true: nothing is mocked, and it is not a Unix PTY.

---

## 2. What shipped

### Wave A — streaming output (the headline fix)

Output was captured into `io.StringIO` and emitted only after `execute()`
returned. Measured before the change:

```
loop of 3 × print + sleep(0.5)  →  returned after 1.2s, all output in one burst
input("Your name? ")            →  prompt invisible until after the answer
```

That second one is a correctness bug, not a polish issue: users were asked
questions they could not see, making the interactive-stdin feature shipped in
the previous release effectively unusable.

New `zmux/streams.py` provides `StreamingWriter`, a `TextIOBase` that pushes
complete lines to the websocket as they are written and flushes partial lines
past 256 chars (so prompts, `end=""` output and `\r` progress bars appear).
`PythonShell` gained an `output_sink` hook. Measured after:

```
ticks now arrive at t=0.0s / 0.5s / 1.0s
input() prompt is on screen while the read blocks
```

Subprocess stdout streams too — `communicate()` replaced with a `readline`
pump on a helper thread, so `ping`/`logcat`-style commands render live.

**Design detail worth knowing:** the result dict still carries the complete
text, so REST callers are byte-for-byte unchanged. A new `streamed` key names
which streams already reached the screen, so the renderer emits only what is
left. Without it, built-in commands (which bypass the sink) would have gone
silent — caught immediately by two existing tests.

### Wave A — environment plumbing

`TerminalSession._build_env()` built a careful environment (`TERM`, `LANG`,
`PATH`, `PYTHONPATH`) and **assigned it to a field nothing ever read**. The
only `Popen` in the codebase passed no `env=` at all.

New `zmux/env.py` is the single builder, now actually passed to children, and
adds `COLORTERM=truecolor`, `TMPDIR` and `PWD`. It documents the Android
zygote passthrough variables (`ANDROID_*`, `BOOTCLASSPATH`, …) that
ReTerminal's `MkSession.kt` shows system binaries depend on.

`_find_executable()` hardcoded six directories and never looked in `BIN_DIR`,
so the `zpip`/`help`/`zmux-info` wrappers `paths.py` carefully generates were
unreachable from the pipeline executor. It now resolves against the same PATH
children receive.

### Wave B — virtual keys, `~/.zmuxrc`, crash hooks

The toolbar was 13 hardcoded `onclick` buttons with **no modifier key at all**,
so `Ctrl+R`, `Ctrl+Z`, `Ctrl+L` were simply untypeable. Rebuilt as two rows
generated from a `KEY_ROWS` array (Termux's data-driven shape, so a
user-supplied layout can be added later without touching markup), with:

- **sticky Ctrl** — tap to latch, tap a key to apply; also covers soft-keyboard
  input via `term.onData`, so `Ctrl+<letter>` works however the letter is typed;
- **hold-to-repeat** on arrows/backspace (400 ms then 55 ms, Rin's timings).

`~/.zmuxrc` runs line by line before the first prompt. ZMUX has no login shell,
so this is the only hook users have for imports and aliases.

`zmux/crash.py` installs `threading.excepthook` + `sys.unraisablehook`. Every
interesting thing ZMUX does happens on a worker thread, and the exec loop's
blanket `except Exception` was discarding tracebacks entirely — exactly the
reports you cannot act on during device testing.

### Wave C — multiple sessions

`SessionManager` owns up to 8 sessions, each with its own `PythonShell` and so
its own cwd, globals, history and running command. Only the active session
writes to the websocket; background sessions keep running and keep appending
to their own scrollback, and switching clears the screen then replays the
target's history rather than interleaving output.

Closing the active tab activates its right-hand neighbour; closing the last one
opens a fresh session so the user is never left with a dead terminal (Rin's
index-clamping idea, made explicit).

Protocol: `session.new` / `session.switch` / `session.close` over the existing
websocket, with a `{"type":"sessions"}` state frame pushed on connect and after
every change. The backend owns all state; the client renders tabs and sends
intents.

### Wave D — zpip UX

Download progress bar (name, MiB, KiB/s, 20-char bar, percent, repainted at
most every 100 ms — rpkg's throttle), rendered **only** when a `progress_sink`
is installed, so no escape codes leak into REST or CLI output.

The install DB now records `explicit`: packages you named versus those pulled
in as dependencies. `zpip list` marks dependencies; `zpip search` marks results
already installed. Records predating the flag default to explicit so nothing is
mislabelled. This is the prerequisite for a future `zpip autoremove`.

**Not copied from rpkg:** its extraction logic. ZMUX's `_safe_members()` is
strictly stronger — Rin's `extract.rs` has no `..` guard at all, no hash
verification in the install path, and no size limits.

---

## 3. Bugs found along the way

Both were pre-existing and neither was in the plan; both were surfaced by
testing the new features.

**`import math` ran ImageMagick.** `_is_external_command()` consulted PATH for
every first word, and ImageMagick ships a binary called `import`. In shell mode
`import math` therefore failed with ``unable to open X server`` instead of
importing the module — the single most common Python statement, broken on any
system with ImageMagick installed. Found while testing `~/.zmuxrc`. Fixed with
a Python-keyword guard.

**Websocket ownership compared bound methods with `is`.** My own new code, in
`PTYTerminalSession.__init__`. Bound methods are rebuilt on each attribute
access, so `self._emit_output is self.ws_server.broadcast` is *always* False —
a standalone session would have registered no input callbacks and accepted no
keyboard input. Caught by checking the assumption in the interpreter rather
than trusting it. Replaced with an explicit flag.

---

## 4. Claims that changed

| Before | Now | Why |
|---|---|---|
| "real subprocesses and **pseudo-terminals (PTY)**" | "virtual terminal, no PTY" | The code never had one. |
| "POSIX openpty with SELinux pipe fallback" | Removed; limitation documented | Same. |
| *(unstated)* | "no `vim`/`htop`/`less`, no job control" | The honest consequence of having no PTY. |
| *(unstated)* | BIN_DIR wrappers may not execute on Android 10+ | W^X blocks `exec()` in the app home dir since targetSdk 29. Flagged as **unverified**, not fixed. |
| "INTERNET only" permission | **unchanged** | See below. |

**On the INTERNET-only boast.** You approved sacrificing it, but it turned out
not to be necessary yet. Multi-session works fully in-process; the permission
would only buy *survival while backgrounded*, which needs a p4a foreground
service (`services = ...:foreground:sticky` + `FOREGROUND_SERVICE` +
`POST_NOTIFICATIONS`). That is a real architectural change to the APK — it
deserves its own wave and its own device testing, not a drive-by. The asset is
still intact, so the decision stays open.

---

## 5. Still open

1. **Foreground service** so sessions survive backgrounding. Costs the
   INTERNET-only claim (approved in principle). Needs device testing.
2. **Verify the `BIN_DIR` wrappers on a real Android 10+ device.** If W^X
   blocks them, the mechanism needs rethinking — the commands still work
   in-process, so this is a convenience path, not a critical one.
3. **`zpip` plan-then-confirm** (`:: Proceed with installation? [Y/n]`), the one
   Wave D item not built: it needs interactive confirmation inside a
   non-interactive `dispatch()` API, so it is a small refactor rather than an
   addition.
4. **`zpip autoremove`**, now unblocked by the `explicit` flag.
5. **Command auto-completion** — the last unticked v1.1.0 item.
6. **Device testing for everything above.** All of this is verified by 222
   automated tests on x86_64 Linux; none of it has run on an ARM phone.
