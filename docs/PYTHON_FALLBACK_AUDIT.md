# PythonShell Fallback Audit

**Status (2026-08-03): audit-only checkpoint. No behavior change.**

The Alpine PTY shell is the product shell. The behavior audited here belongs to
the legacy host-side `PythonShell` compatibility executor used by old REST
`/api/exec`, migration hooks, and tests.

## Current behavior

`PythonShell.execute(line)` has two compatibility paths that can look shell-like:

```text
mistyped command-like line -> command not found (exit 127)
Python expression/statement -> embedded Python execution
```

Examples currently preserved:

```text
gti status                         -> zmux: gti: command not found
foobarbaz                          -> zmux: foobarbaz: command not found
definitely_an_undefined_name + 1   -> Python NameError traceback
ls with force_python=True          -> Python NameError traceback
```

This is **not** Alpine PTY UX. In the real Alpine shell, the shell itself owns
parsing, command lookup, job control, and errors.

## Known guardrail tests

| Behavior | Guardrail |
|---|---|
| Fallback is marked legacy | `app/tests/test_python_fallback_quarantine.py` |
| Mistyped command gets `command not found` | `app/tests/test_env_rc_crash.py::TestCommandNotFound` |
| Python expressions remain Python | `app/tests/test_env_rc_crash.py::TestCommandNotFound::test_python_source_is_not_flagged` |
| Non-TUI unknown expression returns `NameError` | `app/tests/test_python_shell.py::TestKnownTuiCommands::test_non_tui_unknown_word_still_falls_to_python` |
| REPL mode bypasses shell builtins | `app/tests/test_pty_websocket.py::TestPythonReplMode::test_repl_is_pure_python_not_shell` |
| `force_python=True` bypasses command builtins | `app/tests/test_python_shell.py::TestPythonExecution::test_force_python_bypasses_command_builtins` |

## Risk if changed

Changing unknown-line fallback from Python to command-only behavior would affect:

- Old REST `/api/exec` callers that send Python snippets without `python -c`.
- Legacy `~/.zmuxrc` migration hooks that contain Python statements.
- Tests that intentionally protect Python expression semantics.
- Debug workflows that use the legacy app-control console as a Python escape
  hatch.

## Safe migration path

Before changing the default behavior, do the following in separate phases:

1. Use the opt-in `ZMUX_STRICT_HOST_COMMANDS=1` environment flag to preview
   command-only handling for command-like unknown lines.
2. Make REST `/api/exec` callers opt into Python with `python -c` or an explicit
   API field rather than implicit fallback.
3. Add migration warnings for `.zmuxrc` lines that are not app-control commands.
4. Keep REPL mode (`python` / `python3`) pure Python until a replacement exists.
5. Update tests in the same change and document the compatibility break in
   `docs/ALPINE_FIRST_CLEANUP.md`.

Until then, keep the default fallback behavior unchanged and clearly labeled as legacy.
