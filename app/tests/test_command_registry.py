"""Tests for the ZMUX app-control command registry."""


def test_wrapper_registry_has_intentional_categories():
    from zmux import cli, command_registry, paths

    assert cli.COMMANDS == command_registry.WRAPPER_COMMANDS
    assert paths.CLI_COMMANDS == command_registry.WRAPPER_COMMANDS
    assert set(command_registry.PRIMARY_COMMANDS).isdisjoint(command_registry.LEGACY_COMPAT_COMMANDS)
    assert set(command_registry.DIAGNOSTIC_COMMANDS).isdisjoint(command_registry.LEGACY_COMPAT_COMMANDS)


def test_alpine_alias_is_accepted_but_not_generated_as_wrapper():
    from zmux import command_registry, paths

    assert "alpine" in command_registry.ACCEPTED_COMMANDS
    assert "alpine" not in paths.CLI_COMMANDS
    assert command_registry.command_category("alpine") == "legacy"


def test_command_categories_are_explicit():
    from zmux import command_registry

    assert command_registry.command_category("help") == "primary"
    assert command_registry.command_category("zmux-info") == "diagnostic"
    assert command_registry.command_category("zpip") == "legacy"
    assert command_registry.command_category("definitely-not-a-command") == "unknown"


def test_usage_discloses_active_diagnostic_and_legacy_groups(capsys):
    from zmux import cli

    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "active:" in err
    assert "diagnostics:" in err
    assert "legacy compat:" in err
    assert "zpip" in err and "alpine" in err


def test_legacy_linux_wrapper_warns_before_answering(monkeypatch, capsys):
    from zmux import cli, linuxenv

    monkeypatch.setattr(linuxenv, "is_installed", lambda: False)
    assert cli.main(["linux", "apk", "add", "git"]) == 1
    captured = capsys.readouterr()
    assert "legacy host wrapper" in captured.err
    assert "Alpine environment is not installed" in captured.err
