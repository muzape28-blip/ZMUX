# Alpine-First Cleanup Status

**Status (2026-08-03): checkpoint after phases 1–28.**

ZMUX is now treated as an Alpine-first Android terminal. The embedded Python
runtime remains the app/backend bridge for WebView, WebSocket, PTY/session
management, PRoot/bootstrap, Android storage integration, diagnostics, and
legacy compatibility.

This document records what has been quarantined, what must remain for now, and
what should happen before any destructive removal.

## Product contract

```text
User-facing shell      = Alpine Linux in a real PTY
Package workflow      = apk + Python venv/pip inside Alpine
Android bridge        = embedded Python backend
Legacy compatibility  = kept, labeled, tested, and isolated
```

Normal user package examples must look like this:

```sh
apk search <name>
apk add <package>
python3 -m venv ~/.venv
. ~/.venv/bin/activate
python3 -m pip install <name>
```

They must not route users to `zpip install ...` or `linux apk add ...`.

## Completed quarantine phases

| Phase | Area | Current status |
|---|---|---|
| 1 | User-facing wording/docs/issues | Help text, issue templates, app README, and Alpine docs now point to apk + venv/pip. |
| 2 | Runtime diagnostics | `runtime_info.py` owns `zmux-info`; `zpip.py` keeps compatibility aliases only. |
| 3 | REST `/api/exec` | Marked as legacy compatibility; health is side-effect-free; frontend is guarded against using REST exec. |
| 4 | CLI wrappers | Commands are categorized as primary, diagnostic, or legacy compatibility. Legacy commands warn on use. |
| 5 | Package paths | Core runtime dirs are separated from legacy ZABAWHEELS/zpip package dirs. |
| 6 | Package CI pipeline | Build-package workflow is explicitly labeled legacy and retained for migration/historical artifacts. |
| 7 | Docs archive staging | `docs/README.md` maps current vs historical docs; archive staging exists but files are not moved yet. |
| 8 | Root README polish | Root README points to the docs map and legacy package-pipeline policy. |
| 9 | Host-console/PythonShell | `PythonShell` and `TerminalSession` are marked legacy compatibility executors. |
| 10 | `~/.zmuxrc` | Marked as legacy host-side migration hook; Alpine shell customization belongs in `~/.profile`. |
| 11 | `pty.toggle` | Backend remains compatible, UI does not expose it, and detach shows a legacy warning. |
| 12 | Cleanup checkpoint | `ALPINE_FIRST_CLEANUP.md` records quarantined surfaces, guardrails, and removal prerequisites. |
| 13 | Runtime info legacy package labeling | `zmux-info` labels legacy user packages/installed DB as compatibility-only instead of current package workflow. |
| 14 | `zpip` dispatch metadata | `zpip.dispatch()` annotates all results as legacy compatibility while keeping command output stable. |
| 15 | REST `zpip` metadata | `/api/exec` responses for `zpip ...` expose additive legacy metadata from dispatch results. |
| 16 | REST status/input/prompt/stop labeling | Legacy REST session endpoints expose additive compatibility metadata so they are not mistaken for the Alpine PTY state API. |
| 17 | REST compatibility docs | `REST_COMPATIBILITY.md` documents current bootstrap/health endpoints vs legacy REST terminal-session endpoints. |
| 18 | Python fallback quarantine | Host-console unknown-line fallback to Python is marked legacy compatibility while behavior remains unchanged. |
| 19 | Python fallback audit | `PYTHON_FALLBACK_AUDIT.md` records current behavior, guardrail tests, and risks before any behavior change. |
| 20 | Strict host-command preview | Optional `ZMUX_STRICT_HOST_COMMANDS=1` lets tests/callers preview command-only handling without changing the default fallback behavior. |
| 21 | REST exec Python audit | `/api/exec` implicit Python fallback is documented and responses expose additive input-mode metadata. |
| 22 | REST exec language contract design | `REST_EXEC_LANGUAGE_CONTRACT.md` proposes explicit future `language` values without changing server behavior. |
| 23 | REST exec language scaffold | `/api/exec` accepts explicit `language="legacy-auto"`, rejects unknown values, and reserves `python`/`command` before implementation. |
| 24 | REST exec explicit Python | `/api/exec` implements `language="python"` as an explicit embedded-Python path while keeping default `legacy-auto` unchanged. |
| 25 | REST exec command-mode audit | `REST_EXEC_COMMAND_MODE_AUDIT.md` records requirements and risks before implementing `language="command"`. |
| 26 | REST exec command mode | `/api/exec` implements `language="command"` as explicit command-only handling without implicit Python fallback. |
| 27 | REST exec language migration guide | `REST_EXEC_LANGUAGE_MIGRATION.md` documents examples and migration guidance for legacy REST callers. |
| 28 | README REST boundary polish | Root/app READMEs now point developers to WebSocket Alpine PTY as product path and `/api/exec` language docs for legacy clients. |

## Current modules and ownership

| Module/area | Owner/status |
|---|---|
| `app/zmux/pty_session.py` | Product WebSocket terminal bridge; Alpine PTY is primary. Legacy app-control console remains fallback only. |
| `app/zmux/realpty.py` | Core PTY process runner; keep. |
| `app/zmux/linuxenv.py` | Core Alpine/PRoot lifecycle; keep. |
| `app/zmux/storage.py`, `javabridge.py` | Core Android bridge; keep. |
| `app/zmux/runtime_info.py` | Core diagnostics; keep. |
| `app/zmux/server.py` | Core WebView server + legacy REST compatibility; keep, continue isolating `/api/exec` and legacy REST session endpoints. |
| `app/zmux/terminal.py` | Legacy REST executor; do not promote as product shell. |
| `app/zmux/python_shell.py` | Legacy host-side compatibility executor; do not promote as product shell. |
| `app/zmux/zpip.py` | Legacy package manager; compatibility only. |
| `.github/workflows/build-package.yml` | Legacy wheelhouse workflow; retained for migration/historical artifacts. |

## Compatibility surfaces that still exist

These are intentionally not removed yet:

```text
REST /api/exec
TerminalSession
PythonShell
zpip command and module
host-side pip wrapper
linux/alpine host wrappers
~/.zmuxrc
legacy user_packages/installed/staging/downloads paths
legacy package workflows/scripts/tests
pty.toggle backend action
```

Every item above should remain clearly labeled as compatibility or legacy while
it exists.

## Removal prerequisites

Before deleting any legacy surface, verify the relevant prerequisites.

### Before removing `zpip.py`

- `server.py` no longer imports or dispatches `zpip`.
- `cli.py` no longer accepts the `zpip` legacy command.
- `paths.py`/`env.py` no longer need `USER_PACKAGES_DIR` importability.
- `runtime_info` no longer reports legacy installed-package DB as a primary
  diagnostic field.
- Tests under `app/tests/test_zpip.py` and `app/tests/test_search_index.py` are
  either removed or replaced with Alpine-first equivalents.
- Docs and issue templates do not mention `zpip` except as historical context.

### Before removing `PythonShell` / `TerminalSession`

- No production server endpoint instantiates `TerminalSession` for normal UI
  operation.
- Any necessary diagnostics/storage/bootstrap commands have Alpine-safe or
  app-control replacements.
- REST compatibility contract is intentionally broken or versioned.
- Tests that currently cover host-side execution are rewritten as legacy-only
  or removed with an explicit migration note.

### Before removing package pipeline files

- `validate.yml` no longer runs recipe/source-lock/package dry-run checks.
- Workflow/template sync tests are updated.
- `packages/`, package schemas, and package scripts are no longer used as test
  fixtures.
- Historical docs are archived or linked from `docs/README.md`.

### Before disabling `pty.toggle`

- No frontend element exposes the action.
- Tests prove Alpine auto-start/restart covers the normal lifecycle.
- Any debugging replacement is documented for maintainers.

## Guardrail tests added during cleanup

The cleanup is protected by tests including:

```text
app/tests/test_runtime_info.py
app/tests/test_command_registry.py
app/tests/test_legacy_package_paths.py
app/tests/test_host_console_quarantine.py
app/tests/test_python_fallback_quarantine.py
tests/test_python_fallback_audit_doc.py
app/tests/test_rc_quarantine.py
tests/test_rest_compatibility_doc.py
tests/test_rest_exec_python_audit_doc.py
tests/test_rest_exec_language_contract_doc.py
tests/test_rest_exec_language_migration_doc.py
tests/test_rest_exec_command_mode_audit_doc.py
tests/test_legacy_package_pipeline.py
tests/test_docs_index.py
tests/test_readme_alpine_first.py
tests/test_readme_rest_boundary.py
```

These tests intentionally preserve compatibility while preventing new user-facing
references to the retired workflows.

## Maintainer rules

1. Prefer labeling and isolation before deletion.
2. Keep existing user data and migration paths working until removal is planned.
3. Do not add new features to legacy `zpip`, host-console, or package-wheelhouse
   surfaces.
4. Do not present `linux <command...>` or `zpip install ...` as current user UX.
5. If a change makes a legacy surface more visible, add a warning or test guard.
6. Run the full validation suite before moving to a more destructive phase.
