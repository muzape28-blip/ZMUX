"""Tests for ZMUX runtime diagnostics decoupled from legacy zpip."""


def test_runtime_fingerprint_shape():
    from zmux import runtime_info

    fp = runtime_info.runtime_fingerprint()
    assert "schema_version" in fp
    assert "app_version" in fp
    assert "runtime_id" in fp
    assert "python" in fp
    assert "android" in fp
    assert "build_contract" in fp
    assert "paths" in fp
    assert "storage" in fp
    assert "installed" in fp
    assert fp["legacy_package_db"]["status"] == "compatibility-only"
    assert fp["legacy_package_db"]["manager"] == "zpip"


def test_format_fingerprint_contains_diagnostic_header():
    from zmux import runtime_info

    text = runtime_info.format_fingerprint(runtime_info.runtime_fingerprint())
    assert text.startswith("ZMUX Runtime Fingerprint\n")
    assert "Legacy user packages:" in text
    assert "Legacy package DB:  compatibility-only" in text
    assert "Installed packages (legacy zpip):" in text


def test_zpip_keeps_compatibility_aliases_only():
    """Legacy imports stay alive while new diagnostics live in runtime_info."""
    from zmux import runtime_info, zpip

    assert zpip.runtime_fingerprint is runtime_info.runtime_fingerprint
    assert zpip.format_fingerprint is runtime_info.format_fingerprint
    assert zpip.android_abi is runtime_info.android_abi


def test_cli_zmux_info_uses_runtime_info(monkeypatch, capsys):
    from zmux import cli, runtime_info

    monkeypatch.setattr(runtime_info, "runtime_fingerprint", lambda: {
        "app_version": "test",
        "build": "marker",
        "python": {"version": "3.x", "implementation": "CPython", "soabi": "", "ext_suffix": "", "pointer_bits": 64},
        "android": {"abi": "x86_64", "api": 0},
        "runtime_id": "runtime-test",
        "build_contract": {"p4a_commit": "p4a", "ndk": "ndk"},
        "paths": {"cwd": "/tmp", "user_packages": "/tmp/pkgs"},
        "storage": {"free_bytes": 123},
        "installed": [],
    })
    monkeypatch.setattr(runtime_info, "format_fingerprint", lambda fp: "runtime-info-from-new-module")

    assert cli.main(["zmux-info"]) == 0
    assert capsys.readouterr().out == "runtime-info-from-new-module\n"
