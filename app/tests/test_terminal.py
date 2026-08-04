"""Tests for ZMUX terminal execution engine."""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Setup path
APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))

from zmux.terminal import TerminalSession, ProcessStatus
from zmux.paths import BIN_DIR, CLI_COMMANDS, ensure_cli_wrappers


@pytest.fixture
def terminal():
    """Create a fresh terminal session."""
    return TerminalSession()


class TestBuiltinCommands:
    """Test built-in commands."""

    def test_pwd(self, terminal):
        """Test pwd command."""
        result = terminal.execute("pwd")
        assert result["ok"]
        assert result["exit_code"] == 0
        # Python-native mode reports the real app-private working directory.
        assert result["stdout"].strip() == str(terminal.cwd)

    def test_cd_home(self, terminal):
        """Test cd to home."""
        result = terminal.execute("cd")
        assert result["ok"]
        assert result["exit_code"] == 0

    def test_cd_nonexistent(self, terminal):
        """Test cd to non-existent directory."""
        result = terminal.execute("cd /nonexistent/path/12345")
        assert not result["ok"]
        assert result["exit_code"] == 1
        assert "No such file" in result["stderr"] or "cannot access" in result["stderr"]

    def test_clear(self, terminal):
        """Test clear command."""
        result = terminal.execute("clear")
        assert result["ok"]
        assert "\033[2J" in result["stdout"]

    def test_help(self, terminal):
        """Test help command."""
        result = terminal.execute("help")
        assert result["ok"]
        assert "ZMUX Terminal" in result["stdout"]
        assert "help" in result["stdout"]

    def test_exit(self, terminal):
        """Test exit command."""
        result = terminal.execute("exit")
        assert result["ok"]
        assert result["status"] == ProcessStatus.EXITED

    def test_empty_command(self, terminal):
        """Test empty command."""
        result = terminal.execute("")
        assert result["ok"]
        assert result["exit_code"] == 0


class TestRealCommands:
    """Test real subprocess execution."""

    def test_echo(self, terminal):
        """Test echo command."""
        result = terminal.execute("echo hello")
        assert result["ok"]
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0

    def test_ls(self, terminal):
        """Test ls command."""
        result = terminal.execute("ls")
        assert result["ok"]
        assert result["exit_code"] == 0

    def test_python_version(self, terminal):
        """Test python version."""
        result = terminal.execute("python3 --version")
        assert result["ok"]
        assert "Python" in result["stdout"]

    def test_python_c(self, terminal):
        """Test python -c."""
        result = terminal.execute('python3 -c "print(2+2)"')
        assert result["ok"]
        assert "4" in result["stdout"]

    def test_exit_code_nonzero(self, terminal):
        """Test command with non-zero exit code."""
        result = terminal.execute("exit 42")
        assert not result["ok"]
        assert result["exit_code"] == 42

    def test_stderr(self, terminal):
        """Test stderr output."""
        result = terminal.execute("python3 -c 'import sys; print(\"error\", file=sys.stderr)'")
        assert result["ok"]

    def test_invalid_command(self, terminal):
        """Test invalid command."""
        result = terminal.execute("this_command_does_not_exist_12345")
        assert not result["ok"]
        assert result["exit_code"] != 0


class TestWorkingDirectory:
    """Test working directory persistence."""

    def test_cwd_persistence(self, terminal):
        """Test that cwd persists between commands."""
        # Create a test directory
        test_dir = terminal.cwd / "test_dir"
        test_dir.mkdir(exist_ok=True)
        
        # Change to it
        terminal.execute(f"cd {test_dir}")
        
        # Verify cwd changed
        assert terminal.cwd == test_dir
        
        # Execute another command - cwd should still be test_dir
        result = terminal.execute("pwd")
        assert "test_dir" in result["stdout"]
        
        # Cleanup
        test_dir.rmdir()

    def test_path_traversal_protection(self, terminal):
        """Test that built-in cd prevents traversal outside home."""
        # Try to cd outside home
        result = terminal.execute("cd /etc")
        assert not result["ok"]
        assert "outside home" in result["stderr"]


class TestProcessControl:
    """Test process control (stop, input)."""

    def test_stop_idle(self, terminal):
        """Test stopping when no process is running."""
        result = terminal.stop()
        assert result["ok"]

    def test_send_input_idle(self, terminal):
        """Test sending input when no process is running."""
        result = terminal.send_input("test")
        assert not result["ok"]
        assert "No process running" in result["error"]


class TestPrompt:
    """Test prompt generation."""

    def test_prompt_format(self, terminal):
        """Test prompt format."""
        prompt = terminal.get_prompt()
        assert prompt.startswith("zmux:")
        assert prompt.endswith("$ ")

    def test_prompt_shows_path(self, terminal):
        """Test that prompt shows current path."""
        prompt = terminal.get_prompt()
        assert "~" in prompt  # Should show home as ~


class TestTimeout:
    """Test command timeout."""

    def test_timeout(self, terminal):
        """Test command timeout."""
        # sleep for 10s with 0.5s timeout should fail
        result = terminal.execute("sleep 10", timeout=0.5)
        assert not result["ok"]
        assert "timed out" in result.get("stderr", "").lower() or result["exit_code"] == -1


class TestStatus:
    """Test status tracking."""

    def test_initial_status(self, terminal):
        """Test initial status is idle."""
        assert terminal.status == ProcessStatus.IDLE

    def test_status_after_command(self, terminal):
        """Test status after successful command."""
        terminal.execute("echo test")
        assert terminal.status == ProcessStatus.IDLE

    def test_status_after_failed_command(self, terminal):
        """Test status after failed command."""
        terminal.execute("false")
        # Terminal returns to IDLE after command completes (ready for next command)
        assert terminal.status == ProcessStatus.IDLE


class TestCliWrappers:
    """Test transparent Unix shell integration (BIN_DIR wrappers)."""

    def test_bin_dir_exists(self):
        """BIN_DIR is created on zmux.paths import."""
        assert BIN_DIR.is_dir()

    def test_wrappers_generated_on_import(self):
        """Every ZMUX command has a wrapper generated at import time."""
        for name in ("zpip", "help", "zmux-info", "clear", "pip"):
            assert (BIN_DIR / name).is_file(), f"missing wrapper for {name}"

    def test_wrappers_cover_cli_commands(self):
        """ensure_cli_wrappers() covers every declared CLI command."""
        ensure_cli_wrappers()
        for name in CLI_COMMANDS:
            assert (BIN_DIR / name).is_file()

    def test_wrappers_are_executable(self):
        """Wrappers are chmod'ed 0o755."""
        for name in CLI_COMMANDS:
            wrapper = BIN_DIR / name
            mode = stat.S_IMODE(wrapper.stat().st_mode)
            assert mode == 0o755, f"{name} has mode {oct(mode)}"
            assert os.access(wrapper, os.X_OK)

    def test_wrapper_content(self):
        """Wrappers use the Android shell and exec python -m zmux.cli "$0" "$@"."""
        content = (BIN_DIR / "zpip").read_text(encoding="utf-8")
        assert content.startswith("#!/system/bin/sh")
        assert 'exec python -m zmux.cli "$0" "$@"' in content
        assert 'exec python3 -m zmux.cli "$0" "$@"' in content

    def test_ensure_cli_wrappers_idempotent(self):
        """Re-running wrapper generation keeps files and PATH intact."""
        path_before = os.environ["PATH"]
        ensure_cli_wrappers()
        ensure_cli_wrappers()
        assert os.environ["PATH"] == path_before
        for name in CLI_COMMANDS:
            assert (BIN_DIR / name).is_file()

    def test_bin_dir_prepended_to_process_path(self):
        """BIN_DIR is exposed on the current process PATH."""
        assert str(BIN_DIR) in os.environ.get("PATH", "").split(os.pathsep)

    def test_build_env_prepends_bin_dir(self, terminal):
        """Child PTY environments get BIN_DIR at the front of PATH."""
        env = terminal._build_env()
        assert env["PATH"].split(os.pathsep)[0] == str(BIN_DIR)

    @pytest.mark.skipif(shutil.which("sh") is None, reason="requires a POSIX sh")
    def test_wrapper_runs_through_sh(self):
        """Execute a generated wrapper via sh (as the Android shebang would)."""
        env = os.environ.copy()
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            ["sh", str(BIN_DIR / "help")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "ZMUX Terminal" in result.stdout


class TestZmuxCli:
    """Test the python -m zmux.cli entrypoint behind the wrappers."""

    def _run_cli(self, *args, extra_env=None):
        """Run zmux.cli in a subprocess exactly like a wrapper would."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(APP_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "zmux.cli", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_cli_help_main(self, capsys):
        """help prints the formatted ZMUX terminal help text."""
        from zmux import cli
        assert cli.main(["help"]) == 0
        out = capsys.readouterr().out
        assert "ZMUX Terminal" in out
        assert "zpip search <query>" in out
        assert out.endswith("\n")

    def test_cli_clear_main(self, capsys):
        """clear emits the ANSI reset sequences."""
        from zmux import cli
        assert cli.main(["clear"]) == 0
        assert capsys.readouterr().out == "\033[H\033[2J\033[3J"

    def test_cli_zmux_info_main(self, capsys):
        """zmux-info prints the formatted runtime fingerprint."""
        from zmux import cli
        assert cli.main(["zmux-info"]) == 0
        out = capsys.readouterr().out
        assert "ZMUX Runtime Fingerprint" in out
        assert "Runtime ID:" in out

    def test_cli_zpip_main(self, capsys):
        """zpip dispatch output is formatted cleanly."""
        from zmux import cli
        assert cli.main(["zpip", "list"]) == 0
        assert "No packages installed" in capsys.readouterr().out

    def test_cli_zpip_error_exit_code(self, capsys):
        """Failed zpip commands surface a non-zero exit code."""
        from zmux import cli
        assert cli.main(["zpip", "verify", "not-installed-pkg"]) == 1
        assert "verification failed" in capsys.readouterr().out

    def test_cli_no_arguments(self, capsys):
        """Running without a command prints usage and fails."""
        from zmux import cli
        assert cli.main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_cli_unknown_command(self, capsys):
        """Unknown commands are rejected with a clear error."""
        from zmux import cli
        assert cli.main(["definitely-not-a-command"]) == 127
        err = capsys.readouterr().err
        assert "unknown command" in err

    def test_cli_command_name_from_wrapped_path(self, capsys):
        """$0 may be a full wrapper path; only the basename matters."""
        from zmux import cli
        assert cli.main([str(BIN_DIR / "clear")]) == 0
        assert capsys.readouterr().out == "\033[H\033[2J\033[3J"

    def test_cli_help_subprocess(self):
        """python -m zmux.cli help works as a real process."""
        result = self._run_cli("help")
        assert result.returncode == 0
        assert "ZMUX Terminal" in result.stdout

    def test_cli_clear_subprocess(self):
        """python -m zmux.cli clear emits the exact ANSI sequences."""
        result = self._run_cli("clear")
        assert result.returncode == 0
        assert result.stdout == "\033[H\033[2J\033[3J"

    def test_cli_zpip_subprocess(self, tmp_path):
        """python -m zmux.cli zpip search works as a real process.

        Fully hermetic: the subprocess runs offline against a seeded curated
        catalog cache inside a throwaway ANDROID_PRIVATE app dir, so no test
        ever depends on the network or repo state.
        """
        import json
        import time
        from zmux import zpip as zpip_mod

        fp = zpip_mod.runtime_fingerprint()
        cache_name = zpip_mod._catalog_cache_path(
            fp["runtime_id"], fp["android"]["abi"]).name
        cache_dir = tmp_path / "cache" / "catalogs"
        cache_dir.mkdir(parents=True)
        (cache_dir / cache_name).write_text(json.dumps({
            "fetched_at": time.time(),
            "packages": {
                "requests": {"name": "requests", "version": "2.32.3",
                             "summary": "Python HTTP for Humans."},
            },
        }))
        result = self._run_cli(
            "zpip", "search", "requests",
            extra_env={"ANDROID_PRIVATE": str(tmp_path), "ZMUX_OFFLINE": "1"},
        )
        assert result.returncode == 0
        assert "requests 2.32.3 [curated]" in result.stdout

    def test_cli_zmux_info_subprocess(self):
        """python -m zmux.cli zmux-info works as a real process."""
        result = self._run_cli("zmux-info")
        assert result.returncode == 0
        assert "ZMUX Runtime Fingerprint" in result.stdout

    def test_cli_pip_fallback_message(self, capsys, monkeypatch):
        """Without a runnable host interpreter, pip points users to Alpine."""
        from zmux import cli
        monkeypatch.setattr(sys, "executable", "/nonexistent/python")
        assert cli.main(["pip", "install", "requests"]) == 1
        captured = capsys.readouterr()
        out = captured.out
        assert "Host-side pip is not part of the Alpine-first ZMUX workflow" in out
        assert "python3 -m venv ~/.venv" in out
        assert "python3 -m pip install <name>" in out
        assert "zpip install <name>" not in out
        assert "WARNING:" not in captured.err

    def test_cli_pip_delegates_to_standard_pip(self, capfd):
        """With a runnable interpreter, standard pip is invoked."""
        from zmux import cli
        if not (sys.executable and os.access(sys.executable, os.X_OK)):
            pytest.skip("no runnable interpreter")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True,
                timeout=30,
            )
        except OSError:
            pytest.skip("interpreter cannot be executed")
        if proc.returncode != 0:
            pytest.skip("standard pip is not importable")
        assert cli.main(["pip", "--version"]) == 0
        assert "pip" in capfd.readouterr().out

    def test_format_output_is_shared(self):
        """Server keeps a lazy wrapper around the legacy zpip formatter."""
        from zmux import server
        output, exit_code = server._format_zpip_output(
            "zpip install demo",
            {"ok": True, "package": "demo", "version": "1.0", "dependencies_installed": []},
        )
        assert exit_code == 0
        assert output == "Successfully installed demo-1.0"

    def test_format_fingerprint_matches_server_shape(self):
        """zpip.format_fingerprint renders the zmux-info text for both faces."""
        from zmux import zpip
        text = zpip.format_fingerprint(zpip.runtime_fingerprint())
        assert text.startswith("ZMUX Runtime Fingerprint\n")
        assert "Installed packages (legacy zpip):" in text
