# Legacy ZABAWHEELS Package Pipeline

**Status (2026-08-03): retained for migration and historical artifacts.**

ZMUX's user-facing package workflow is now Alpine-first:

```sh
apk search <name>
apk add <package>
python3 -m venv ~/.venv
. ~/.venv/bin/activate
python3 -m pip install <name>
```

The old ZABAWHEELS wheelhouse pipeline is still present because parts of the
repository and test suite use it as a reproducibility/security fixture. It is
not the direction for new user-facing package features.

## What remains, and why

| Area | Current status | Reason it remains |
|---|---|---|
| `.github/workflows/build-package.yml` | Legacy workflow | Can still reproduce historical wheel artifacts when explicitly dispatched. |
| `workflow-templates/build-package.yml` | Legacy workflow template | Kept in sync with the GitHub workflow until the workflow is removed. |
| `packages/` | Legacy recipes/smoke package | Test fixture for recipe validation and native-wheel build scripts. |
| `schemas/package-manifest.schema.json` / `schemas/recipe.schema.json` | Legacy schemas | Validate the retained recipe/manifests while tests still cover them. |
| `scripts/build.py`, `generate_index.py`, `generate_manifest.py`, `validate_recipes.py`, `verify_source_lock.py` | Legacy package tooling | Used by repository validation and historical reproducibility checks. |
| `app/zmux/zpip.py` | Runtime compatibility | Old installs/tests may still call it; user guidance should prefer Alpine. |

## Rules for maintainers

1. Do **not** add new user-facing features to `zpip` or the package-wheel
   workflow.
2. Do **not** route Alpine users back to `zpip`; use `apk` and venv-local
   `pip` examples.
3. Keep the legacy workflow explicitly labeled while it exists.
4. Keep workflow templates and `.github/workflows/*` byte-for-byte synchronized.
5. Removal should happen only after runtime imports, tests, docs, and release
   expectations no longer depend on the package pipeline.

## Removal prerequisites

Before deleting the legacy pipeline, verify all of the following:

- `server.py`, `cli.py`, `paths.py`, `env.py`, and `terminal.py` no longer need
  `zpip` compatibility paths.
- `runtime_info` no longer reports the legacy installed-package database as a
  primary diagnostic field.
- App tests have Alpine-first replacements for current `zpip` and package-index
  assertions.
- GitHub workflows no longer run package recipe/source-lock validation as part
  of the default repository validation path.
- Historical documentation has been archived under `docs/archive/` or clearly
  marked as legacy.

Until then, the pipeline should stay available but visibly quarantined.
