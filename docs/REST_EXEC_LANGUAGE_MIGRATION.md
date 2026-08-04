# REST `/api/exec` Language Migration Guide

**Status (2026-08-03): compatibility guide for legacy REST callers.**

The interactive ZMUX terminal uses WebSocket + Alpine PTY. This guide is only
for existing clients that still call legacy `POST /api/exec`.

## Recommended direction

For interactive terminal UX, migrate to the WebSocket Alpine PTY path described
in [REST_COMPATIBILITY.md](REST_COMPATIBILITY.md). Do not build new terminal
features on `/api/exec`.

For compatibility clients that must keep using `/api/exec`, make intent explicit
with the `language` field.

## Modes

| Mode | Use when | Python fallback? |
|---|---|---|
| `legacy-auto` | You need current backward-compatible behavior. | Yes, legacy auto command-or-Python. |
| `python` | You intentionally want embedded Python execution. | No fallback needed; it is explicitly Python. |
| `command` | You intentionally want command-like handling only. | No implicit Python fallback. |

Payloads without `language` behave as `legacy-auto` for compatibility, but new
callers should send an explicit value.

## Examples

### Legacy auto, explicit

```json
{
  "command": "1 + 1",
  "language": "legacy-auto"
}
```

This preserves old behavior and returns Python output through the legacy auto
path:

```text
2
```

### Explicit Python

```json
{
  "command": "print(21 + 21)",
  "language": "python"
}
```

Expected output:

```text
42
```

Shell builtins are not intercepted in this mode. For example:

```json
{
  "command": "ls",
  "language": "python"
}
```

returns a Python `NameError` rather than a directory listing.

### Explicit command

```json
{
  "command": "echo hello",
  "language": "command"
}
```

Expected output:

```text
hello
```

Unknown command-like input returns command-not-found semantics:

```json
{
  "command": "gti status",
  "language": "command"
}
```

Expected result:

```text
zmux: gti: command not found
```

Python expressions are not evaluated in command mode:

```json
{
  "command": "1 + 1",
  "language": "command"
}
```

This must not return `2`.

## Response metadata

Compatibility responses include metadata such as:

```json
{
  "legacy": true,
  "legacy_endpoint": true,
  "legacy_status": "compatibility-only",
  "legacy_input_mode": "legacy-auto-command-or-python",
  "explicit_language": true,
  "language": "command"
}
```

Use this metadata in clients to detect old REST behavior and guide users toward
Alpine/WebSocket workflows.

## Migration checklist

1. Stop using `/api/exec` for interactive terminal UX.
2. If keeping `/api/exec`, add an explicit `language` field.
3. Use `language="python"` for Python snippets.
4. Use `language="command"` for command-like snippets that must not fall back to
   Python.
5. Keep `language="legacy-auto"` only where exact backward compatibility is
   required.
6. Do not parse human-readable `stdout` warnings; use metadata fields instead.

## Related docs

- [REST_COMPATIBILITY.md](REST_COMPATIBILITY.md)
- [REST_EXEC_LANGUAGE_CONTRACT.md](REST_EXEC_LANGUAGE_CONTRACT.md)
- [REST_EXEC_PYTHON_AUDIT.md](REST_EXEC_PYTHON_AUDIT.md)
- [REST_EXEC_COMMAND_MODE_AUDIT.md](REST_EXEC_COMMAND_MODE_AUDIT.md)
