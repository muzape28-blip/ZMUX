# REST `/api/exec` Python Fallback Audit

**Status (2026-08-03): audit-only checkpoint. No behavior change.**

`POST /api/exec` is a legacy compatibility endpoint. It still accepts command
strings that are interpreted by the legacy `TerminalSession` / `PythonShell`
path. That path can execute both command-like input and Python snippets through
implicit fallback behavior.

The product terminal does **not** use this endpoint. The product path is the
WebSocket Alpine PTY session documented in [REST_COMPATIBILITY.md](REST_COMPATIBILITY.md).

## Current behavior

The endpoint currently accepts a payload like:

```json
{"command": "1 + 1"}
```

and returns Python output from the legacy executor:

```json
{
  "ok": true,
  "stdout": "2\n",
  "legacy": true,
  "legacy_input_mode": "legacy-auto-command-or-python",
  "explicit_language": false
}
```

This is retained for old callers only. New clients should not depend on implicit
Python fallback through `/api/exec`.

## Compatibility metadata

All `/api/exec` responses should include additive metadata:

| Field | Meaning |
|---|---|
| `legacy` | This is a compatibility endpoint. |
| `legacy_endpoint` | The response came from legacy REST terminal-session code. |
| `legacy_status` | Usually `compatibility-only`. |
| `legacy_input_mode` | The endpoint accepts legacy auto command-or-Python strings. |
| `explicit_language` | `false` today; there is no explicit language selector yet. |
| `deprecation` | Human-readable guidance to use the WebSocket Alpine PTY path. |

## Risk if changed

Changing implicit Python fallback in `/api/exec` would affect:

- Old callers that submit Python snippets directly.
- Tests that verify legacy compatibility behavior.
- `.zmuxrc` and host-console migration paths that still use `PythonShell`.
- Debug tooling that has not moved to explicit `python -c` or WebSocket PTY.

## Safe migration path

1. Keep metadata while the endpoint exists.
2. Add an explicit language/API contract before removing implicit fallback; see
   [REST_EXEC_LANGUAGE_CONTRACT.md](REST_EXEC_LANGUAGE_CONTRACT.md) for the
   design-only proposal.
3. Add warnings/metadata first; avoid changing `stdout` unexpectedly.
4. Update [PYTHON_FALLBACK_AUDIT.md](PYTHON_FALLBACK_AUDIT.md) and
   [ALPINE_FIRST_CLEANUP.md](ALPINE_FIRST_CLEANUP.md) in the same change.

Until then, `/api/exec` remains compatibility-only and should not be used for new
interactive terminal UX.
