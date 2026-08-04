# Package Compatibility Matrix

> **Legacy ZABAWHEELS note (2026-08-03):** this matrix belongs to the retained
> wheelhouse/package-pipeline era. ZMUX's current user-facing package workflow
> is Alpine `apk` plus Python venv/pip inside the Alpine shell.
Runtime: zmux-py314-api26-p4a5c192d7b7308-r1
Last Updated: 2026-07-29
Status: Initial tracking - No native packages built yet

## Status Definitions

| Status | Meaning | User Visibility |
|--------|---------|-----------------|
| planned | Package identified, not yet started | Hidden |
| researching | Checking compatibility | Hidden |
| built | Cross-compilation successful | Hidden |
| inspected | ELF and metadata verified | Hidden |
| installable | Installer can install artifact | Candidate |
| imported | Package can be imported | Candidate |
| device-verified | Tested on real device | Stable |
| stable | Production ready | Stable |
| blocked | Upstream/toolchain issue | Hidden |

## Priority Packages

### Tier 1: Core Scientific Computing
| Package | Version | ARMv7 | ARM64 | Status | Notes |
|---------|---------|-------|-------|--------|-------|
| numpy | 2.0.0 | Pending | Pending | planned | Core numerical computing |
| scipy | 1.12.0 | Pending | Pending | planned | Scientific computing |
| pandas | 2.2.0 | Pending | Pending | planned | Data analysis |

### Tier 2: Data Processing
| Package | Version | ARMv7 | ARM64 | Status | Notes |
|---------|---------|-------|-------|--------|-------|
| pillow | 10.2.0 | Pending | Pending | planned | Image processing |
| requests | 2.31.0 | Yes | Yes | stable | Pure Python |
| lxml | 4.9.0 | Pending | Pending | planned | XML parsing |

### Tier 3: Security
| Package | Version | ARMv7 | ARM64 | Status | Notes |
|---------|---------|-------|-------|--------|-------|
| cryptography | 42.0.0 | Pending | Pending | planned | Encryption |

## Statistics
- Total: 7 packages tracked
- Stable: 1 (requests)
- Planned: 6
- Blocked: 0

## How to Add a Package
1. Research if package has native extensions
2. Create build recipe in packages/<name>/
3. Test cross-compilation in CI
4. Inspect ELF and metadata
5. Publish to candidate index
6. Test on device
7. Promote to stable
