# ZMUX compatibility contract

## Active runtime generation

```text
zmux-py314-api26-p4a5c192d7b7308-r1
```

The build inputs are locked in `toolchain/runtime-lock.json` and enforced by
`app/buildozer.spec` and the APK workflow:

- CPython recipe: 3.14.2
- python-for-Android: `5c192d7b7308487c2d3e3fcae63deba3131e7cb2`
- Android NDK: 28c; NDK API: 26
- target/minimum API: 34/26
- ABIs: armeabi-v7a and arm64-v8a
- Buildozer: 1.5.0

SOABI and extension suffix are ABI-specific at runtime. The lock records the
contract family; ZMUX's authenticated `GET /api/runtime` endpoint exports the
exact values from the installed APK. Native package selection uses that live
fingerprint and never ABI guessing.

## Runtime report

The endpoint reports:

```json
{
  "runtime_id": "zmux-py314-api26-p4a5c192d7b7308-r1",
  "python": {
    "implementation": "CPython",
    "version": "3.14.2",
    "soabi": "<value reported by sysconfig>",
    "ext_suffix": "<value reported by sysconfig>"
  },
  "android": {
    "abi": "armeabi-v7a or arm64-v8a",
    "api": 34,
    "pointer_bits": 32
  }
}
```

A device report, not CI alone, is required before setting `device-verified`.

## Strict matching

A native wheel is compatible only if all of these agree:

```text
runtime_id + CPython ABI/SOABI + Android ABI + minimum API
```

Universal `py3-none-any` wheels follow a separate pure-Python path. A Linux,
wrong-ABI, or wrong-runtime wheel is never renamed or silently attempted.

## Generation changes

Create a new generation (`-r2`, `-r3`, …) when CPython, SOABI, p4a, NDK,
minimum API, native library layout, dynamic loader policy, or ABI-affecting
compiler flags change. Existing artifacts remain immutable.

## Verification status

- **armeabi-v7a:** APK build target; physical Infinix verification pending.
- **arm64-v8a:** APK build target; physical community verification pending.

Neither is described as device-verified until its report is committed and
validated against `schemas/device-report.schema.json`.
