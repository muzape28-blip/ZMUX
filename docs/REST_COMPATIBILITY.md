# REST Compatibility Boundary

**Status (2026-08-03): REST terminal-session endpoints are compatibility-only.**

ZMUX's interactive terminal is not driven by REST command execution. The product
path is:

```text
terminal.html -> authenticated WebSocket -> SessionManager -> PTYTerminalSession -> real PTY -> PRoot -> Alpine
```

The REST API remains for WebView bootstrap, health checks, and compatibility
with older callers. Do not build new terminal UX on `/api/exec`.

## Current/non-legacy REST surface

| Endpoint | Status | Notes |
|---|---|---|
| `GET /` | Current | Serves `terminal.html` with WebSocket token/port. |
| `GET /api/health` | Current | Side-effect-free readiness check. It must not create the legacy `TerminalSession`. |

## Legacy compatibility REST surface

These endpoints report the legacy Python/REST session, not the Alpine PTY tab
state. Responses include additive metadata such as `legacy`, `legacy_endpoint`,
`legacy_status`, and `deprecation`.

| Endpoint | Status | Notes |
|---|---|---|
| `POST /api/exec` | Legacy compatibility | Executes through the legacy REST path; `zpip ...` and `zmux-info` have special compatibility handling. |
| `POST /api/input` | Legacy compatibility | Sends input to the legacy REST session placeholder, not the WebSocket Alpine PTY. |
| `POST /api/stop` | Legacy compatibility | Stops the legacy REST session placeholder. |
| `GET /api/status` | Legacy compatibility | Reports `TerminalSession`/`PythonShell` state, not active Alpine tabs. |
| `GET /api/prompt` | Legacy compatibility | Reports the legacy `zmux:~$` style prompt. |

## Rules for maintainers

1. New interactive terminal features should use WebSocket session actions or raw
   PTY input, not `/api/exec`.
2. `/api/health` must remain side-effect-free and must not instantiate
   `TerminalSession`.
3. If a legacy REST endpoint remains, keep additive legacy/deprecation metadata
   in the JSON response.
4. If a legacy REST endpoint is removed later, document the version/contract
   break and update tests in the same change.
5. Do not describe `/api/status` or `/api/prompt` as Alpine PTY state unless
   they are explicitly rewired to the WebSocket `SessionManager`.

## Removal prerequisites

Before removing or changing the legacy REST session endpoints:

- Existing WebView flows must be proven not to call them.
- Tests must cover the WebSocket/PTY replacement for any lost behavior.
- Compatibility clients must receive a documented migration path.
- `docs/ALPINE_FIRST_CLEANUP.md` must be updated with the new state.
