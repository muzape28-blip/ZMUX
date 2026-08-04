# ZMUX Kotlin Native Terminal UI — PoC (P1a)

Proof-of-concept replacement for the current **WebView + xterm.js** UI with a **native
Kotlin terminal** built on Termux' Apache-2.0 `terminal-view` / `terminal-emulator`
libraries, talking to the **existing Python ZMUX backend over WebSocket**.

> **The PTY engine is not touched.** `realpty.py`, `linuxenv.py`, `zpip`, PRoot/Alpine and
> the Buildozer pipeline stay exactly as they are. This module only replaces the pixels.
>
> **Why Kotlin but not Rust:** adopting `terminal-view` also gives us Termux'
> `terminal-emulator` (ANSI parser, grid, scrollback) under Apache-2.0 — which is precisely the
> ~5,500 lines Rin's Rust core exists to provide. Kotlin removes the last reason to add Rust.
> Full reasoning in [`docs/KOTLIN_RUST_DECISION.md`](../../docs/KOTLIN_RUST_DECISION.md).

---

## 1. What is here

```
experimental/zmux-kotlin/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
├── gradle/wrapper/gradle-wrapper.properties
├── tools/
│   ├── mock_pty_ws_server.py         # mock backend speaking the REAL contract
│   └── protocol_check.py             # raw RFC-6455 conformance gates (9/9)
└── app/
    ├── build.gradle.kts
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/zmux/terminal/
        │   ├── ZmuxProtocol.kt           # the contract, annotated with Python line refs
        │   ├── WebSocketPtyBridge.kt     # OkHttp WS <-> emulator plumbing
        │   ├── ZmuxTerminalSession.kt    # TerminalSession with NO local fork()
        │   ├── ZmuxTerminalView.kt       # thin wrapper over Termux TerminalView
        │   ├── ZmuxViewClient.kt         # TerminalViewClient (sticky Ctrl/Alt)
        │   ├── ZmuxKeys.kt               # virtual keys ported 1:1 from terminal.html
        │   ├── ZmuxBackendLocator.kt     # token discovery + WS port probing
        │   └── ZmuxTerminalActivity.kt   # host activity, tab strip, key bar
        └── res/                          # layout, theme, cleartext-localhost config
```

### Architecture

```
 ┌────────────────────────── Android app (Kotlin) ──────────────────────────┐
 │  ZmuxTerminalView (Termux TerminalView, Apache-2.0)                      │
 │        │  keystrokes                    ▲ screen updates                 │
 │        ▼                                │                                │
 │  ZmuxTerminalSession  ── write() ──►  WebSocketPtyBridge (OkHttp)         │
 │        ▲  feed()  ◄──────────────────────────┘                           │
 └────────────────────────────────│─────────────────────────────────────────┘
                                  │  ws://127.0.0.1:8001/ws
 ┌────────────────────────────────▼─────────────────────────────────────────┐
 │  Python ZMUX backend — ws_server.py → realpty.py → PRoot/Alpine → shell   │
 └──────────────────────────────────────────────────────────────────────────┘
```

The key trick is `ZmuxTerminalSession`: it subclasses `TerminalSession` but overrides
`initializeEmulator()` so **no local shell is forked** on the Android side. Only the
`TerminalEmulator` (screen buffer + VT parser) is created; all I/O is proxied to the
Python PTY.

---

## 2. Wire protocol — transcribed from the Python source

Verified against `app/zmux/ws_server.py`, `sessions.py`, `security.py` and `server.py`
in [ZABAWHEELS](https://github.com/muzape28-blip/ZABAWHEELS). Full annotation with line
references lives in `ZmuxProtocol.kt`.

### Auth is a query parameter

    ws://127.0.0.1:<WS_PORT>/?token=<AUTH_TOKEN>

`_verify_token()` parses the request line and compares with `hmac.compare_digest`.
A bad token gets **HTTP 401 before the upgrade** — a handshake failure, not a close frame,
and it must NOT be retried.

The token is generated at runtime by `security.py` into `APP_DIR/.zmux_auth_token` (mode
0600). The WS port is dynamic too: `server.py` prefers `HTTP_PORT + 1` then falls back.

**Client → server** — the discriminator is **`action`**, not `type`

| Frame | Meaning |
|---|---|
| binary/text | raw keystroke bytes for the PTY |
| `{"action":"resize","cols":C,"rows":R}` | `TIOCSWINSZ` on the master |
| `{"action":"session.new"}` | create + switch |
| `{"action":"session.switch","id":"…"}` | switch active session |
| `{"action":"session.close","id":"…"}` | close session |
| `{"action":"session.list"}` | re-push the snapshot |
| `{"action":"pty.toggle"}` | Alpine PTY ↔ ZMUX host console |

> JSON is only treated as control when the trimmed text starts with `{` **and** ends with
> `}`. Anything else is PTY input, so typing a literal `{...}` in the shell still works.

**Server → client**

| Frame | Handling |
|---|---|
| binary (opcode 2) | raw PTY output → emulator |
| text `{"type":"sessions",...}` | tab-strip state |
| text, anything else | plain output → emulator |

On connect the server replays, in order: `RESET_TERMINAL_SCREEN`
(`\x1b[?1049l\x1b[?47l\x1b[?1047l\x1b[2J\x1b[H`, so a reconnect during Vim/less does not
inherit a stale alt-buffer), then the scrollback, then a `sessions` snapshot.

---

## 3. Build

Requirements: Android Studio Koala+ (AGP 8.5, Kotlin 1.9.24, JDK 17), Android SDK 34.

```bash
cd experimental/zmux-kotlin
# open this folder as the project root in Android Studio, or:
./gradlew :app:assembleDebug        # after `gradle wrapper` generates gradlew
```

The wrapper JAR/script is intentionally **not** committed (binary). Generate it once:

```bash
cd experimental/zmux-kotlin
gradle wrapper --gradle-version 8.7
```

Dependencies (`app/build.gradle.kts`):

- `com.termux.termux-app:terminal-view:0.118.0` (JitPack, Apache-2.0)
- `com.termux.termux-app:terminal-emulator:0.118.0` (JitPack, Apache-2.0)

Per termux-app's *Termux Libraries* wiki page the JitPack groupId is `com.termux.termux-app`
(`com.termux` also resolves via a DNS TXT record). `terminal-emulator` is transitive but
pinned explicitly so version skew fails at resolution, not runtime.
- `com.squareup.okhttp3:okhttp:4.12.0`
- AndroidX core/appcompat/material — kept minimal for Android Go

Targets: `minSdk 26`, `compileSdk/targetSdk 34`, ABIs `armeabi-v7a` + `arm64-v8a`
(Infinix Smart 9 HD class).

> If JitPack resolution for `com.termux.termux-app:*` fails in your environment, the
> fallback is to vendor the two Apache-2.0 modules (`terminal-view/`, `terminal-emulator/`)
> from `termux/termux-app` as Gradle subprojects. **Do not** copy code from the GPLv3
> Termux application module.

---

## 4. Run & test

### 4.1 Against the real backend (same device)

1. Run the ZMUX backend (`python main.py`, or the Buildozer APK). Note the port it prints:
   `[INFO] Selected WebSocket Port: <N>`.
2. Get the token from a debuggable build:
   ```bash
   adb shell run-as com.zabawheels.zmux cat files/.zmux_auth_token
   ```
3. Install this PoC, fill in host / port / token, tap **Connect**.

### 4.2 Against the mock server (desktop / emulator)

```bash
python3 tools/mock_pty_ws_server.py --host 0.0.0.0 --port 8001 --token dev
```

It mirrors the real contract: `?token=` auth with 401, `action` routing, the connect-time
reset + sessions replay. From an emulator use host `10.0.2.2`.

### 4.3 Automated protocol verification

`tools/protocol_check.py` speaks raw RFC-6455 and asserts exactly the behaviours the Kotlin
bridge depends on:

```bash
python3 tools/mock_pty_ws_server.py --port 8011 --token dev &
python3 tools/protocol_check.py --port 8011 --token dev
```

Result on 2026-08-04:

```
[PASS] auth1-bad-token-401: status=401
[PASS] auth2-good-token-101: status=101
[PASS] replay1-reset-screen: RESET_TERMINAL_SCREEN received
[PASS] replay2-sessions-frame: {"type":"sessions","sessions":[{"id":"1","busy":false}],"active":"1","max":8}
[PASS] replay3-sessions-shape: max=8
[PASS] resize1-stty-size: b'stty size\r\n11 47\r\n'
[PASS] input1-shell-executes: b'echo ZMUX_$((6*7))\r\nZMUX_42\r\n'
[PASS] sigint1-ctrl-c: 0x03 interrupted foreground sleep
[PASS] session1-list-reply: 1 new text frame(s)

9/9 gates passed
```

`resize1` and `sigint1` are the meaningful ones: both prove bytes reach the **kernel line
discipline**, not just an echo loop.

**Point this at the real `ws_server.py` next** — if it passes 9/9 there too, the bridge is
correct.

### 4.4 Manual checklist on device

- [ ] `ls`, `python3`, `top` render (TUI needs correct resize)
- [ ] rotation reflows the shell
- [ ] `^C` interrupts `sleep 30`
- [ ] sticky CTRL + `c` from the soft keyboard also sends `0x03`
- [ ] tab strip switches sessions; long-press closes
- [ ] backend restart → `reconnecting` → recovers

## 5. Known issues

- **Not compiled.** No JDK/Android SDK in the authoring sandbox, so `assembleDebug` has never
  run. Expect signature drift in `TerminalViewClient` / `TerminalSessionClient` against the
  exact `terminal-view` tag you resolve — Termux changes these between releases.
- **Gradle wrapper not committed** — run `gradle wrapper --gradle-version 8.7` once.
- **Cross-APK token access is impossible by design.** Android app-private storage is per-UID,
  so a separate Kotlin APK cannot read the Python app's `.zmux_auth_token`. Supply it manually
  during development. This is the strongest argument for the Chaquopy step, which makes UI and
  engine share one UID and deletes the problem.
- **Port is dynamic.** `server.py` may not land on 8001. `ZmuxBackendLocator.probeWsPort()`
  scans the fallback range, but the connect bar still lets you set it by hand.
- Text selection / clipboard callbacks are stubs.
- No foreground Service yet — a backgrounded backend can still be killed by the OS
  (`REFERENCE_MINING.md` T2).

## 6. Next steps

1. **Align protocol** with the real `app/zmux/ws_server.py` (binary vs JSON, auth handshake).
2. **Session manager**: multiple PTYs, tab bar, matching `zmux-info`.
3. **Configurable extra-keys row** (JSON-defined, Termux pattern from `docs/REFERENCE_MINING.md`).
4. **Chaquopy migration (P1b)**: embed the Python engine in-process and replace
   `WebSocketPtyBridge` with a direct `realpty` call — deleting the socket layer while
   keeping `ZmuxTerminalSession` unchanged. That is the whole point of isolating the
   transport behind one class.
5. Theme/font parity with the xterm.js UI; then retire the WebView.

---

## 7. Licensing

- This module: **AGPL-3.0**, same as ZMUX.
- `terminal-view` / `terminal-emulator`: **Apache-2.0** (Termux) — used as binary
  dependencies. No GPLv3 Termux app code is copied into this repo.
