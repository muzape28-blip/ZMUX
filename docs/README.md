# ZMUX Documentation Index

**Status (2026-08-03): Alpine-first documentation map.**

ZMUX's current user-facing environment is Alpine Linux in a real PTY. The
embedded Python runtime remains the Android/WebView/PTY/PRoot bridge. This
index separates current docs from historical ZABAWHEELS/package-pipeline notes
so maintainers do not accidentally route users back to retired workflows.

## Current / actively maintained

| Document | Purpose |
|---|---|
| [BUILDING.md](BUILDING.md) | Buildozer/APK build notes and PRoot build entry point. |
| [COMPATIBILITY.md](COMPATIBILITY.md) | Runtime compatibility contract. |
| [PROOT_ALPINE.md](PROOT_ALPINE.md) | Alpine/PRoot architecture and on-device constraints. |
| [PYTHON_FALLBACK_AUDIT.md](PYTHON_FALLBACK_AUDIT.md) | Audit of legacy PythonShell fallback behavior and risks before changing it. |
| [REST_COMPATIBILITY.md](REST_COMPATIBILITY.md) | REST endpoint boundary: current health/bootstrap vs legacy terminal-session API. |
| [REST_EXEC_PYTHON_AUDIT.md](REST_EXEC_PYTHON_AUDIT.md) | Audit of legacy `/api/exec` implicit Python fallback and metadata. |
| [REST_EXEC_LANGUAGE_CONTRACT.md](REST_EXEC_LANGUAGE_CONTRACT.md) | Validation scaffold and contract for explicit `/api/exec` language selection. |
| [REST_EXEC_LANGUAGE_MIGRATION.md](REST_EXEC_LANGUAGE_MIGRATION.md) | Compatibility guide for legacy REST callers migrating to explicit `language` values. |
| [REST_EXEC_COMMAND_MODE_AUDIT.md](REST_EXEC_COMMAND_MODE_AUDIT.md) | Audit/maintenance requirements for `language="command"` behavior. |
| [SECURITY.md](SECURITY.md) | WebView, loopback, storage, and package-security threat model. |
| [DEVICE_TESTING.md](DEVICE_TESTING.md) | Device test checklist; older rows are labeled where legacy-era. |
| [ALPINE_FIRST_CLEANUP.md](ALPINE_FIRST_CLEANUP.md) | Cleanup checkpoint: quarantined legacy surfaces, guardrails, and removal prerequisites. |
| [LEGACY_PACKAGE_PIPELINE.md](LEGACY_PACKAGE_PIPELINE.md) | Quarantine policy for retained ZABAWHEELS wheelhouse tooling. |

## Mixed current + historical implementation notes

These contain useful engineering decisions and debugging history, but some
sections predate Alpine-first UX. Treat cleanup notes at the top of each file as
a guide before quoting them in user-facing docs.

| Document | Notes |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Contains current server/PTY details plus legacy host-console/API history. |
| [CAPABILITY_REPORT.md](CAPABILITY_REPORT.md) | Executed capability report from an earlier runtime state. |
| [DEVICE_FAILURE_ANALYSIS.md](DEVICE_FAILURE_ANALYSIS.md) | On-device failure analysis; references storage/proot/zpip history. |
| [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) | Reference-mining implementation log. |
| [POST_FIX_REPORT.md](POST_FIX_REPORT.md) | Historical post-fix validation report. |
| [REFERENCE_MINING.md](REFERENCE_MINING.md) | Research report that informed terminal/session improvements. |
| [RUST_KOTLIN_ANALYSIS.md](RUST_KOTLIN_ANALYSIS.md) | Rust/Kotlin adoption analysis. |
| [WORKFLOW_VERIFY_STEPS.md](WORKFLOW_VERIFY_STEPS.md) | Workflow-hardening note retained for provenance. |

## Legacy ZABAWHEELS package/wheelhouse docs

These are retained for migration and historical artifacts. They are not the
current package recommendation for ZMUX users.

| Document | Current interpretation |
|---|---|
| [PACKAGE_COMPATIBILITY.md](PACKAGE_COMPATIBILITY.md) | Legacy package compatibility matrix. Prefer Alpine `apk` and venv/pip for users. |
| [PACKAGE_LIFECYCLE.md](PACKAGE_LIFECYCLE.md) | Legacy package lifecycle model for the retained wheelhouse pipeline. |
| [../ZABAWHEELS.md](../ZABAWHEELS.md) | Original foundational architecture/roadmap for the wheelhouse era. |

## Archive staging

No files are moved yet, to preserve links while cleanup is ongoing. When a doc
is fully historical and no tests/releases link to it as current guidance, move
it under [archive/](archive/) with a short replacement pointer.

## Maintainer rules

1. User-facing package examples should use Alpine `apk` and Python virtual
   environments, not `zpip`.
2. Do not delete historical docs until links, tests, workflows, and release
   notes are updated.
3. If a document mixes current and legacy guidance, add a cleanup note instead
   of silently rewriting history.
4. Keep this index updated whenever docs are added, archived, or reclassified.
5. For shell startup customisation, document Alpine `~/.profile`; `~/.zmuxrc`
   is a legacy host-side migration hook only.
