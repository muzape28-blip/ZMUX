# Proposed REST `/api/exec` Language Contract

**Status (2026-08-03): validation scaffold.**

`language: "legacy-auto"` is accepted as an explicit alias for today's default
behavior. `language: "python"` explicitly executes embedded Python. `language:
"command"` explicitly executes command-like input without implicit Python
fallback. Unknown language values are rejected.

`POST /api/exec` is a legacy compatibility endpoint. Today it accepts only a
`command` string and sends that string through the legacy `TerminalSession` /
`PythonShell` auto-dispatch path. That means a single field can behave as a
command, app-control command, `zpip` request, or Python snippet.

This document sketches a future explicit language contract so old callers can
migrate without abruptly breaking the compatibility endpoint.

## Current payload

```json
{
  "command": "1 + 1"
}
```

Current metadata says this is implicit legacy behavior:

```json
{
  "legacy_input_mode": "legacy-auto-command-or-python",
  "explicit_language": false
}
```

## Language payloads

### Legacy auto mode

Equivalent to today's behavior. This remains the default while the legacy
endpoint exists and is the only implemented explicit language value today.

```json
{
  "command": "echo hello",
  "language": "legacy-auto"
}
```

Current metadata when explicitly requested:

```json
{
  "legacy_input_mode": "legacy-auto-command-or-python",
  "explicit_language": true,
  "language": "legacy-auto"
}
```

### Explicit Python mode

For callers that intentionally want embedded Python execution through the
legacy REST endpoint.

```json
{
  "command": "print(21 + 21)",
  "language": "python"
}
```

Current semantics:

```text
execute as Python intentionally, not because of unknown-command fallback
shell/app-control builtins are bypassed
```

### Explicit command mode

For compatibility callers that want command-like behavior without implicit
Python fallback.

```json
{
  "command": "gti status",
  "language": "command"
}
```

Current semantics:

```text
known command -> command result
command-like unknown input -> command not found
Python expressions are not implicitly evaluated
```

## Non-goals

- This does not make REST `/api/exec` the product terminal API.
- This does not replace the WebSocket Alpine PTY path.
- This does not change default behavior yet.
- This does not remove `PythonShell`, `TerminalSession`, or `zpip`.

## Implementation guardrails

Before changing or extending this contract further:

1. Keep the current default payload compatible.
2. Continue validating `language` strictly; unknown values return HTTP 400 with
   a clear error code.
3. Keep response metadata for explicit language selection.
4. Keep `/api/health` side-effect-free.
5. Do not change `stdout` for legacy-auto responses unexpectedly.
6. Update [REST_EXEC_PYTHON_AUDIT.md](REST_EXEC_PYTHON_AUDIT.md) and
   [ALPINE_FIRST_CLEANUP.md](ALPINE_FIRST_CLEANUP.md) in the same change.

## Suggested accepted values

```text
legacy-auto  current compatibility behavior
python       explicit embedded Python execution
command      strict command-like handling, no implicit Python fallback
```

`command` mode requirements were audited in
[REST_EXEC_COMMAND_MODE_AUDIT.md](REST_EXEC_COMMAND_MODE_AUDIT.md) before
implementation and should stay in sync with any future changes.

The names are intentionally verbose so JSON payloads remain self-explanatory in
bug reports and client code.
