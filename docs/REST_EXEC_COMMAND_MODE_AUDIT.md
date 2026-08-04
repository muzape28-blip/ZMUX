# REST `/api/exec` Command Mode Audit

**Status (2026-08-03): implemented after audit checkpoint.**

`POST /api/exec` is a legacy compatibility endpoint. The product terminal is the
WebSocket Alpine PTY; new interactive terminal UX must not be built on REST
command execution.

This document defined the requirements and risks for explicit
`language="command"` mode before behavior was implemented. Keep it as the
maintenance checklist for command-mode changes.

## Goal

Future `language="command"` should provide command-like handling without implicit
Python fallback:

```json
{
  "command": "gti status",
  "language": "command"
}
```

Expected future result:

```text
zmux: gti: command not found
```

A Python expression should not be implicitly evaluated in command mode:

```json
{
  "command": "1 + 1",
  "language": "command"
}
```

Expected future result should be a command-style error, not Python output.

## Current status

Today:

```text
language absent         -> legacy-auto behavior
language="legacy-auto" -> legacy-auto behavior
language="python"      -> explicit embedded Python
language="command"     -> explicit command-only behavior
```

## Requirements

A safe command mode must continue to satisfy all of these:

1. **No implicit Python fallback.** Command mode must not call the legacy
   unknown-line Python fallback.
2. **Known builtins still answer.** Commands like `echo`, `pwd`, `clear`, and
   compatibility app-control names should still return deterministic results.
3. **Unknown command-like input returns 127.** Typo-style input such as
   `gti status` should return command-not-found semantics.
4. **Python expressions are not evaluated.** Input such as `1 + 1` must not
   return `2` in command mode.
5. **Default behavior remains unchanged.** Payloads without `language` must keep
   legacy-auto semantics until a separate breaking-change phase.
6. **Metadata remains explicit.** Responses should include `language: "command"`,
   `explicit_language: true`, and legacy/deprecation metadata.
7. **No WebSocket regression.** The Alpine PTY path must remain the product
   terminal and must not route through REST command mode.

## Candidate implementation strategy

One conservative implementation path is:

1. Add a `TerminalSession.execute_command(...)` method that calls a restricted
   PythonShell command path.
2. Reuse the existing command tables for builtins/app-control commands.
3. Reuse `PythonShell._looks_like_command()` and `_command_not_found()` for
   unknown command-like input.
4. For obvious Python expression input, return command-style `command not found`
   or a clear `not a command in language="command"` error without evaluating it.
5. Keep `language="python"` as the explicit way to run embedded Python.

This should be implemented in a separate phase with tests before enabling any
broader migration.

## Guardrail tests

Keep tests for:

```text
/api/exec {command: "echo hello", language: "command"} -> hello
/api/exec {command: "gti status", language: "command"} -> 127 command not found
/api/exec {command: "1 + 1", language: "command"} -> not Python output
/api/exec {command: "ls", language: "python"} -> Python NameError
/api/exec {command: "1 + 1"} -> legacy-auto still returns 2
```

If any of those guarantees change, update this audit, the REST language
contract, and server tests in the same phase.
