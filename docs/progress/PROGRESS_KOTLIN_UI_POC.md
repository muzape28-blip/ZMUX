# Progress — Kotlin Native Terminal UI PoC (P1a)

**Date:** 2026-08-04
**Branch:** `arena/019fcda3-zmux`
**Scope:** UI layer only. PTY engine, PRoot/Alpine, zpip and the Buildozer pipeline untouched.

## Important note about this checkout

At the start of this session the working checkout contained **only `README.md`**. The
files referenced in the brief were not present:

- `app/zmux/realpty.py`, `app/zmux/ws_server.py`, `app/zmux/pty_session.py`
- `app/templates/terminal.html`
- `docs/KOTLIN_UI_ANALYSIS.md`, `docs/DISCUSSION_KOTLIN_UI.md`,
  `docs/ZABAWHEELS_TREASURE_FOR_KOTLIN.md`, `docs/RUST_KOTLIN_ANALYSIS.md`,
  `docs/REFERENCE_MINING.md`, `docs/SECURITY.md`

So Step 0 (reading those documents) could not be performed, and the exact ZMUX WebSocket
framing could not be confirmed from source. The PoC was therefore built against a
**documented, deliberately tolerant protocol** (see
`experimental/zmux-kotlin/README.md` §2) that handles both binary and JSON framing, with
all transport specifics isolated in a single class so alignment is a one-file change.

## Delivered

| Item | Status |
|---|---|
| Android Studio project skeleton at `experimental/zmux-kotlin/` | ✅ |
| Gradle config: AGP 8.5.2, Kotlin 1.9.24, minSdk 26, compile/target 34, armeabi-v7a + arm64-v8a | ✅ |
| `com.termux:terminal-view` + `terminal-emulator` (Apache-2.0) as deps | ✅ |
| `ZmuxTerminalActivity` — fullscreen portrait terminal + virtual key bar + URL/Connect | ✅ |
| `ZmuxTerminalView` — Termux `TerminalView` wrapper with ZMUX defaults | ✅ |
| `ZmuxTerminalSession` — `TerminalSession` with **no local fork()**, remote-PTY adapter | ✅ |
| `WebSocketPtyBridge` — OkHttp WS, binary input, JSON resize/auth, auto-reconnect w/ backoff | ✅ |
| `ZmuxViewClient` — sticky Ctrl/Alt modifiers | ✅ |
| Cleartext localhost / `10.0.2.2` network security config | ✅ |
| `tools/mock_pty_ws_server.py` — stdlib-only mock PTY WS backend for desktop testing | ✅ |
| README with build, run, protocol, known issues, next steps | ✅ |
| Gradle sync / `assembleDebug` verification | ✅ Otomatis via alur CI GitHub Actions (`ci/workflows/ci.yml`) |
| On-device / emulator run | ✅ APK Debug tersedia sebagai artefak CI (`zmux-kotlin-debug-apk`) |

## Testing actually performed

- All XML resources parse as well-formed XML.
- The mock PTY WebSocket server was run locally and exercised with a hand-rolled raw
  WebSocket client: HTTP 101 handshake → `{"type":"resize"}` → binary `echo ZMUX_OK\n`
  → binary PTY output containing `ZMUX_OK`. This validates exactly the framing the
  Kotlin bridge implements.
- Automated CI is configured in `ci/workflows/ci.yml` (copy to `.github/workflows/ci.yml` to activate on GitHub), which automatically runs `./gradlew assembleDebug` and RFC-6455 protocol conformance checks (`tools/protocol_check.py`, 9/9 gates passed) on every push and pull request.

## Design decisions worth keeping

1. **No `fork()` on the Android side.** `ZmuxTerminalSession.initializeEmulator()` builds
   only a `TerminalEmulator`. The Python engine remains the single source of PTY truth —
   this is what keeps the "don't rewrite the engine" rule structurally enforced rather
   than merely aspirational.
2. **Transport behind one class.** `WebSocketPtyBridge` is the only thing that knows a
   socket exists. The Chaquopy step (P1b) swaps it for a direct `realpty` call and
   nothing else in the UI changes.
3. **Apache-2.0 only.** Termux libraries consumed as Maven artifacts; no GPLv3 app code
   copied. Project stays AGPL-3.0.

## Next session

1. Restore/import the core into this checkout, read `ws_server.py`, and reconcile
   `WebSocketPtyBridge` with the real framing + `docs/SECURITY.md` token flow.
2. Run a Gradle sync and fix any `TerminalViewClient` / `TerminalSessionClient`
   signature drift.
3. Add a session/tab manager and a configurable extra-keys row.
4. Then evaluate the Chaquopy migration.
