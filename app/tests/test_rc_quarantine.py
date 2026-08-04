"""Guardrails for quarantining the legacy ~/.zmuxrc host hook."""


def test_rc_hook_is_marked_legacy_and_profile_is_current_shell_startup():
    from zmux import paths

    assert paths.LEGACY_RC_HOOK is True
    assert paths.RC_FILENAME == ".zmuxrc"
    assert paths.ALPINE_PROFILE_FILENAME == ".profile"
    assert "legacy host-side rc" in (paths.legacy_rc_path.__doc__ or "")
    assert "legacy" in (paths.read_rc_lines.__doc__ or "")
    assert "~/.profile" in (paths.read_rc_lines.__doc__ or "")


def test_legacy_rc_path_uses_home_argument(tmp_path):
    from zmux.paths import RC_FILENAME, legacy_rc_path

    assert legacy_rc_path(tmp_path) == tmp_path / RC_FILENAME


def test_read_rc_lines_still_reads_legacy_hook_for_migration(tmp_path):
    from zmux.paths import legacy_rc_path, read_rc_lines

    legacy_rc_path(tmp_path).write_text("# comment\nvalue = 1\n\nprint(value)\n", encoding="utf-8")
    assert read_rc_lines(tmp_path) == ["value = 1", "print(value)"]


def test_pty_run_rc_doc_points_users_to_alpine_profile():
    from zmux.pty_session import PTYTerminalSession

    doc = PTYTerminalSession._run_rc.__doc__ or ""
    assert "legacy host-side" in doc
    assert "~/.profile" in doc
    assert "not the Alpine shell startup file" in doc


def test_linuxenv_uses_alpine_profile_constant(tmp_path, monkeypatch):
    from zmux import linuxenv
    from zmux.paths import ALPINE_PROFILE_FILENAME

    monkeypatch.setattr(linuxenv, "HOME_DIR", tmp_path)
    linuxenv.ensure_user_home_layout()
    profile = tmp_path / ALPINE_PROFILE_FILENAME
    assert profile.is_file()
    assert "zmux@alpine" in profile.read_text(encoding="utf-8")


def test_user_facing_help_and_readme_do_not_promote_zmuxrc():
    from pathlib import Path
    from zmux.terminal import HELP_TEXT

    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert ".zmuxrc" not in HELP_TEXT
    assert ".zmuxrc" not in readme
    assert "~/.profile" in (root / "docs" / "README.md").read_text(encoding="utf-8")
