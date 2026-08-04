"""Guardrails for the legacy Python fallback in the host compatibility shell."""


def test_python_shell_marks_unknown_line_python_fallback_as_legacy():
    from zmux import python_shell

    assert python_shell.LEGACY_COMPATIBILITY_EXECUTOR is True
    assert python_shell.LEGACY_PYTHON_FALLBACK is True
    assert python_shell.STRICT_HOST_COMMANDS_ENV == "ZMUX_STRICT_HOST_COMMANDS"
    assert "compatibility" in (python_shell.__doc__ or "")
    assert "not the normal user-facing ZMUX shell" in (python_shell.PythonShell.__doc__ or "")


def test_command_classifier_is_documented_as_host_console_compatibility():
    from zmux.python_shell import PythonShell

    doc = PythonShell._looks_like_command.__doc__ or ""
    assert "legacy host-console" in doc
    assert "not Alpine PTY UX" in doc


def test_unknown_expression_still_falls_to_python_for_compatibility(tmp_path):
    from zmux.python_shell import PythonShell

    shell = PythonShell(tmp_path)
    result = shell.execute("definitely_an_undefined_name_zz9 + 1")
    assert result["ok"] is False
    assert "NameError" in result["stderr"]


def test_mistyped_command_still_gets_command_not_found_for_compatibility(tmp_path):
    from zmux.python_shell import PythonShell

    shell = PythonShell(tmp_path)
    result = shell.execute("gti status")
    assert result["exit_code"] == 127
    assert "command not found" in result["stderr"]


def test_strict_host_commands_env_short_circuits_command_like_fallback(tmp_path, monkeypatch):
    from zmux.python_shell import PythonShell

    shell = PythonShell(tmp_path)
    monkeypatch.setenv("ZMUX_STRICT_HOST_COMMANDS", "1")

    def fail_python(*args, **kwargs):
        raise AssertionError("strict command-like input must not fall through to Python")

    monkeypatch.setattr(shell, "_exec_python", fail_python)
    result = shell.execute("gti status")
    assert result["exit_code"] == 127
    assert "gti: command not found" in result["stderr"]


def test_strict_host_commands_env_still_allows_python_expressions(tmp_path, monkeypatch):
    from zmux.python_shell import PythonShell

    monkeypatch.setenv("ZMUX_STRICT_HOST_COMMANDS", "1")
    shell = PythonShell(tmp_path)
    result = shell.execute("definitely_an_undefined_name_zz9 + 1")
    assert result["ok"] is False
    assert "NameError" in result["stderr"]


def test_user_facing_docs_do_not_promote_python_fallback():
    from pathlib import Path
    from zmux.terminal import HELP_TEXT

    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "unknown commands fall back to Python" not in HELP_TEXT
    assert "unknown commands fall back to Python" not in readme
    assert "Python REPL" not in readme
    assert "apk add" in readme
