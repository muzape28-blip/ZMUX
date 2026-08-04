# ZMUX Kotlin Native Terminal UI — PoC (P1a)

Proof-of-concept replacement for the current **WebView + xterm.js** UI with a **native
Kotlin terminal** built on Termux' Apache-2.0 `terminal-view` / `terminal-emulator`
libraries, talking to the **existing Python ZMUX backend over WebSocket**.

> **The PTY engine is not touched.** `realpty.py`, `linuxenv.py`, `zpip`, PRoot/Alpine and
> the Buildozer pipeline stay exactly as they are. This module only replaces the pixels.

---

## 1. What is here

```
experimental/zmux-kotlin/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
├── gradle/wrapper/gradle-wrapper.properties
├── tools/mock_pty_ws_server.py       # stdlib-only mock backend for desktop testing
└── app/
    ├── build.gradle.kts
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/zmux/terminal/
        │   ├── ZmuxTerminalActivity.kt   # host activity + virtual key bar
        │   ├── ZmuxTerminalView.kt       # thin wrapper over Termux TerminalView
        │   ├── ZmuxTerminalSession.kt    # TerminalSession with NO local fork()
        │   ├── ZmuxViewClient.kt         # minimal TerminalViewClient (Ctrl/Alt state)
        │   └── WebSocketPtyBridge.kt     # OkHttp WebSocket <-> emulator plumbing
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

## 2. Wire protocol

Deliberately tolerant so it works with whatever `ws_server.py` currently emits.

**Client → server**

| Frame | Meaning |
|---|---|
| binary | raw keystroke bytes for the PTY (fast path, default) |
| `{"type":"auth","token":"…"}` | sent first when a token is configured (also sent as `Authorization: Bearer`) |
| `{"type":"input","data":"…"}` | text fallback (`Config.useBinaryInput = false`) |
| `{"type":"resize","rows":R,"cols":C}` | sent on attach and on every layout change |

**Server → client**

| Frame | Handling |
|---|---|
| binary | appended verbatim to the emulator |
| `{"type":"output"\|"data"\|"stdout","data":"…"}` | `data` appended to the emulator |
| `{"type":"error","message":"…"}` | surfaced in the status bar |
| `{"type":"exit"}` | status → disconnected |
| `{"type":"ok"\|"pong"\|"auth"}` | ignored |
| any other text | appended verbatim (safe for plain-text servers) |

If the real `ws_server.py` differs, adapt **only** `WebSocketPtyBridge.onMessage` /
`sendInput` — nothing else in the UI depends on the framing.

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

- `com.termux.termux-app:terminal-view:v0.118.0` (JitPack, Apache-2.0)
- `com.termux.termux-app:terminal-emulator:v0.118.0` (JitPack, Apache-2.0)
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

### 4.1 Against the real backend (on-device)

1. Build/install the existing ZMUX app (Buildozer) or run the backend directly:
   ```bash
   python main.py           # or: python -m app.zmux.ws_server
   ```
   Confirm it listens on `127.0.0.1:8001`.
2. Install this PoC APK on the same device.
3. Launch **ZMUX Terminal**, keep the URL `ws://127.0.0.1:8001/ws`, tap **Connect**.

### 4.2 Against the mock server (desktop / emulator)

A stdlib-only mock PTY WebSocket server is included for UI iteration without the
full ZMUX stack:

```bash
python3 experimental/zmux-kotlin/tools/mock_pty_ws_server.py --host 0.0.0.0 --port 8001
```

From the Android emulator use `ws://10.0.2.2:8001/ws` (already allowed in
`network_security_config.xml`).

Verified locally with a raw WebSocket client: handshake → `resize` → binary
`echo ZMUX_OK\n` → binary PTY output containing `ZMUX_OK`. ✅

### 4.3 Manual test checklist

- [ ] `ls`, `echo hi`, `python3` produce real-time output
- [ ] Rotation / keyboard show-hide triggers a `resize` and reflows the shell
- [ ] `^C` virtual key interrupts a running `sleep 30`
- [ ] `CTRL` toggle + `c` from the soft keyboard also sends `0x03`
- [ ] Arrow keys navigate shell history
- [ ] Killing the backend shows `reconnecting` and auto-recovers when it returns

---

## 5. Known issues

- **Gradle wrapper not committed** — run `gradle wrapper` once (see §3).
- **Not compiled in CI yet**: this workspace has no JDK/Android SDK, so the module has
  not been through `assembleDebug`. Expect minor signature adjustments against the exact
  `terminal-view` release you resolve (Termux occasionally changes
  `TerminalViewClient` / `TerminalSessionClient` method sets between tags).
- **Backend protocol is assumed**, not read from a live `ws_server.py` (the file was not
  present in this checkout at the time of writing). See §2 — one file to adapt.
- **No auth UI**: the token is only accepted via the `com.zmux.terminal.TOKEN` intent
  extra. Wire it to `docs/SECURITY.md`'s token source next.
- **Single session only** — no tab/session switcher yet.
- Modifier keys are sticky one-shot; no full extra-keys-row config like Termux.
- Text selection / clipboard callbacks are stubs.

---

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
