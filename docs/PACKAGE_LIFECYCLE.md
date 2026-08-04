# Package Lifecycle

> **Legacy ZABAWHEELS note (2026-08-03):** this lifecycle describes the retained
> wheelhouse/package-pipeline era. Do not use it as current user package
> guidance; Alpine `apk` and venv-local `pip` are the supported workflow.
> **Status:** Pre-Alpha (M0)

## Lifecycle States

Every package in ZABAWHEELS follows a strict lifecycle:

```text
REQUESTED → RESEARCHED → RECIPE READY → BUILDING → BUILT → ELF INSPECTED
→ CANDIDATE → DEVICE TESTED → STABLE
```

Failure paths:

```text
BUILD FAILED | IMPORT FAILED | RUNTIME INCOMPATIBLE | BLOCKED UPSTREAM
| DEPRECATED | REVOKED
```

## Status Definitions

| Status | Meaning | Who sets it |
|---|---|---|
| `planned` | Requested but not yet investigated | Issue submission |
| `researching` | Compatibility being researched | Developer |
| `recipe-ready` | Recipe and initial patches available | Recipe PR |
| `building` | Build process running | CI workflow |
| `built` | Cross-compilation completed successfully | CI workflow |
| `inspected` | ELF and metadata verified | CI inspection |
| `installable` | Installer can place artifact | ZabaPip test |
| `imported` | Package can be imported in Python | Smoke test |
| `smoke-passed` | Basic functions run correctly | Smoke test |
| `device-verified` | Tested on real Android device | Device test report |
| `stable` | Passed all gates for target runtime | Promotion workflow |
| `broken` | Proven not to work | Build or device test |
| `blocked` | Blocked by upstream, toolchain, or dependency | Research |
| `deprecated` | No longer recommended | Maintainer decision |
| `revoked` | Withdrawn for security or serious breakage | Security response |

## Promotion Policy

- **experimental → candidate**: Build and static inspection passed
- **candidate → stable**: Device test and lifecycle test passed
- **stable → revoked**: Security issue or serious breakage found

All promotions must go through pull request or protected workflow with audit trail.

## Required Information per Package

```text
Package:
Version:
Upstream:
License:
Source hash:
Build system:
Native dependencies:
Python versions:
Target ABI:
Expected wheel size:
Known Android issues:
Smoke test:
Current status:
```

## Revocation

When an artifact is revoked, the manifest must include:

```json
{
  "revoked": true,
  "reason": "Crashes on ARMv7 Android 14",
  "replacement": "1.2.1",
  "severity": "high"
}
```

Revoked artifacts remain in GitHub Releases (for audit) but are marked in the index and rejected by ZabaPip.

## Current Package Status (M0)

| Package | Status | Notes |
|---|---|---|
| zaba-native-smoke | planned | First package — feasibility spike |
| package-template | template | Template for new package recipes |

No packages have been built yet. Building requires M1 gate completion.
