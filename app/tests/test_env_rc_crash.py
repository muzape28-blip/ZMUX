"""Tests for the child-process environment, ~/.zmuxrc startup file and crash log."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from zmux import crash, env
from zmux.paths import BIN_DIR, HOME_DIR, RC_FILENAME, read_rc_lines
from zmux.python_shell import PythonShell


class TestEnvironment:
    def test_build_env_sets_terminal_identity(self):
        result = env.build_env()
        assert result["TERM"] == "xterm-256color"
        assert result["COLORTERM"] == "truecolor"
        assert result["LANG"] == "C.UTF-8"
        assert result["HOME"] == str(HOME_DIR)
        assert result["ZMUX"] == "1"

    def test_path_starts_with_bin_dir_then_system_dirs(self):
        parts = env.build_path().split(os.pathsep)
        assert parts[0] == str(BIN_DIR), "BIN_DIR must win so ZMUX wrappers resolve first"
        assert "/system/bin" in parts

    def test_path_has_no_duplicates(self):
        parts = env.build_path().split(os.pathsep)
        assert len(parts) == len(set(parts))

    def test_pythonpath_exposes_installed_packages(self):
        from zmux.paths import USER_PACKAGES_DIR
        assert str(USER_PACKAGES_DIR) in env.build_env()["PYTHONPATH"].split(os.pathsep)

    def test_child_process_receives_the_built_env(self, tmp_path):
        """Regression: the only Popen used to run without env= at all."""
        shell = PythonShell(tmp_path)
        if shell._find_executable("printenv") is None:
            pytest.skip("printenv unavailable on this host")
        result = shell.execute("printenv ZMUX")
        assert result["stdout"].strip() == "1", "child did not inherit the ZMUX env"

    def test_find_executable_searches_bin_dir(self, tmp_path):
        """Regression: BIN_DIR was omitted from the hardcoded search list."""
        marker = BIN_DIR / "zmux_probe_binary"
        marker.write_text("#!/system/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(marker, 0o755)
        try:
            assert PythonShell(tmp_path)._find_executable("zmux_probe_binary") == str(marker)
        finally:
            marker.unlink(missing_ok=True)


class TestRcFile:
    def test_missing_rc_returns_empty(self, tmp_path):
        assert read_rc_lines(tmp_path) == []

    def test_comments_and_blank_lines_are_stripped(self, tmp_path):
        (tmp_path / RC_FILENAME).write_text(
            "# a comment\n\nimport math\n   \n  x = 1  \n", encoding="utf-8"
        )
        assert read_rc_lines(tmp_path) == ["import math", "x = 1"]

    def test_unreadable_rc_never_raises(self, tmp_path):
        (tmp_path / RC_FILENAME).write_bytes(b"\xff\xfe\x00invalid")
        assert isinstance(read_rc_lines(tmp_path), list)

    def test_rc_runs_before_first_prompt(self, tmp_path, monkeypatch):
        from zmux import pty_session

        class _WS:
            def __init__(self): self.data = bytearray()
            def register_callbacks(self, on_data, on_resize): pass
            def broadcast(self, payload): self.data.extend(payload)

        (tmp_path / RC_FILENAME).write_text("rc_value = 99\n", encoding="utf-8")
        monkeypatch.setattr(pty_session, "HOME_DIR", tmp_path)
        session = pty_session.PTYTerminalSession(_WS())
        session.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and session._busy.is_set():
                time.sleep(0.05)
            assert session.shell.globals.get("rc_value") == 99
        finally:
            session.stop()


class TestCrashLog:
    def test_record_writes_an_entry(self):
        path = crash._log_path()
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            raise ValueError("zmux_crash_probe")
        except ValueError as error:
            crash.record("unit-test", type(error), error, error.__traceback__)
        text = path.read_text(encoding="utf-8")
        assert "zmux_crash_probe" in text
        assert "[unit-test]" in text
        path.write_text(before, encoding="utf-8")

    def test_thread_exception_is_recorded(self):
        crash.install()
        path = crash._log_path()
        before = path.read_text(encoding="utf-8") if path.exists() else ""

        def boom():
            raise RuntimeError("zmux_thread_probe")

        worker = threading.Thread(target=boom, name="probe")
        worker.start()
        worker.join()
        text = path.read_text(encoding="utf-8")
        assert "zmux_thread_probe" in text, "worker-thread crash was not persisted"
        path.write_text(before, encoding="utf-8")


class TestPythonKeywordGuard:
    """`import math` must never run ImageMagick's `import` binary.

    Regression: _is_external_command() consulted PATH for every first word,
    so on any system shipping ImageMagick, `import math` failed with
    "unable to open X server" instead of importing the module.
    """

    def test_import_statement_is_python_not_imagemagick(self, tmp_path):
        shell = PythonShell(tmp_path)
        result = shell.execute("import math")
        assert result["exit_code"] == 0, result["stderr"]
        assert "X server" not in result["stderr"]
        assert shell.execute("print(math.pi)")["stdout"].startswith("3.14")

    def test_from_import_statement_works(self, tmp_path):
        shell = PythonShell(tmp_path)
        assert shell.execute("from pathlib import Path")["exit_code"] == 0

    def test_guard_does_not_block_real_external_commands(self, tmp_path):
        shell = PythonShell(tmp_path)
        if shell._find_executable("echo") is None:
            pytest.skip("echo unavailable on this host")
        assert shell._is_external_command("echo") is True

    @pytest.mark.parametrize("keyword", ["import", "print", "class", "while"])
    def test_keywords_are_never_external(self, tmp_path, keyword):
        assert PythonShell(tmp_path)._is_external_command(keyword) is False


class TestCdAffectsPython:
    """`cd` must move in-process Python too, not just subprocesses.

    Regression: the shell tracked a `cwd` variable and passed it to Popen,
    but never called os.chdir(). A user could write a file in Python and
    then find `ls` and `cat` could not see it.
    """

    def test_python_writes_into_the_cd_directory(self):
        # Built-in `cd` is jailed to HOME_DIR, so this must run under it.
        import shutil
        target = HOME_DIR / "cdprobe"
        shutil.rmtree(target, ignore_errors=True)
        shell = PythonShell(HOME_DIR)
        try:
            shell.execute("mkdir -p cdprobe")
            assert shell.execute("cd cdprobe")["exit_code"] == 0
            shell.execute("open('made.txt','w').write('data')", force_python=True)
            assert (target / "made.txt").exists(), "Python ignored cd"
            assert not (HOME_DIR / "made.txt").exists()
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_ls_and_cat_see_python_created_files(self, tmp_path):
        shell = PythonShell(tmp_path)
        shell.execute("open('visible.txt','w').write('hello')", force_python=True)
        assert "visible.txt" in shell.execute("ls")["stdout"]
        assert shell.execute("cat visible.txt")["stdout"] == "hello"

    def test_process_cwd_is_restored_after_execution(self, tmp_path):
        before = os.getcwd()
        PythonShell(tmp_path).execute("open('x.txt','w').write('1')", force_python=True)
        assert os.getcwd() == before, "chdir leaked out of the execution context"

    def test_missing_cwd_does_not_crash(self, tmp_path):
        target = tmp_path / "vanishing"
        target.mkdir()
        shell = PythonShell(target)
        target.rmdir()  # deleted underneath the session
        assert shell.execute("1 + 1", force_python=True)["stdout"].strip() == "2"


class TestUnsupportedOperators:
    """Operators ZMUX lacks must fail loudly, never silently."""

    @pytest.mark.parametrize("line,token", [
        ("/bin/true && /bin/echo x", "&&"),
        ("/bin/false || /bin/echo x", "||"),
        ("/bin/echo a; /bin/echo b", ";"),
        ("/bin/sleep 1 &", "&"),
        ("/bin/echo $(date)", "$(...)"),
        ("/bin/sh -c 'echo x >&2' 2>&1", "2>&1"),
    ])
    def test_operator_is_rejected(self, tmp_path, line, token):
        result = PythonShell(tmp_path).execute(line)
        assert result["exit_code"] == 2, f"{token} was silently accepted"
        assert "not supported" in result["stderr"]
        assert token.split("(")[0] in result["stderr"]

    def test_and_operator_does_not_half_execute(self, tmp_path):
        """The original bug: exit 0 reported, second command never ran."""
        shell = PythonShell(tmp_path)
        result = shell.execute("/bin/true && /usr/bin/touch built.txt")
        assert result["exit_code"] != 0, "reported success for a command it did not run"
        assert not (tmp_path / "built.txt").exists()

    def test_stream_merge_does_not_create_junk_file(self, tmp_path):
        PythonShell(tmp_path).execute("/bin/sh -c 'echo e >&2' 2>&1")
        assert not (tmp_path / "&1").exists(), "created a file literally named '&1'"

    @pytest.mark.parametrize("line", [
        "/bin/echo 'a && b'", "/bin/echo \"x; y\"", "/bin/echo abc | /usr/bin/tr a-z A-Z",
    ])
    def test_quoted_and_supported_syntax_still_works(self, tmp_path, line):
        assert PythonShell(tmp_path).execute(line)["exit_code"] == 0

    @pytest.mark.parametrize("line", ["x = 1; y = 2", "print(1 and 2)", "a = [i for i in range(3)]"])
    def test_python_source_is_not_flagged(self, tmp_path, line):
        assert PythonShell(tmp_path).execute(line)["exit_code"] == 0


class TestCommandNotFound:
    """A mistyped command should read like a shell error, not a traceback."""

    @pytest.mark.parametrize("line,name", [
        ("gti status", "gti"), ("foobarbaz", "foobarbaz"),
        ("npmm install express", "npmm"), ("git-foo --bar", "git-foo"),
        ("curlx https://example.com", "curlx"),
    ])
    def test_typo_reports_command_not_found(self, tmp_path, line, name):
        result = PythonShell(tmp_path).execute(line)
        assert result["exit_code"] == 127
        assert f"{name}: command not found" in result["stderr"]
        assert "Traceback" not in result["stderr"]

    @pytest.mark.parametrize("line", [
        "undefined_var + 1", "missing(1)", "x = = 5",
        "[a for a in bad]", "a if b else c", "y = undefined2",
    ])
    def test_real_python_errors_are_preserved(self, tmp_path, line):
        result = PythonShell(tmp_path).execute(line)
        assert result["exit_code"] != 127, "real Python was misreported as a typo"

    def test_defined_names_still_evaluate(self, tmp_path):
        shell = PythonShell(tmp_path)
        shell.execute("value = 41")
        assert shell.execute("value + 1")["stdout"].strip() == "42"


class TestInstalledPackagesAreImportable:
    """`zpip install X` must make `import X` work in the REPL.

    Regression: USER_PACKAGES_DIR was exported to children via PYTHONPATH but
    never added to the running interpreter's sys.path, so install reported
    success and the very next `import` raised ModuleNotFoundError.
    """

    def test_user_packages_dir_is_on_sys_path(self):
        from zmux.paths import USER_PACKAGES_DIR
        assert str(USER_PACKAGES_DIR) in sys.path

    def test_module_in_user_packages_imports_in_process(self, tmp_path):
        import shutil
        from zmux.paths import USER_PACKAGES_DIR
        probe = USER_PACKAGES_DIR / "zmux_import_probe"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "__init__.py").write_text("VALUE = 'installed'", encoding="utf-8")
        try:
            result = PythonShell(tmp_path).execute(
                "import zmux_import_probe as p; print(p.VALUE)", force_python=True
            )
            assert "installed" in result["stdout"], result["stderr"]
        finally:
            shutil.rmtree(probe, ignore_errors=True)
            sys.modules.pop("zmux_import_probe", None)


class TestModuleDiscovery:
    """Import names must come from the artifact, not from a guess."""

    @staticmethod
    def _wheel(tmp_path, layout):
        import zipfile
        staging = tmp_path / "staging"
        staging.mkdir(exist_ok=True)
        for rel, content in layout.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return staging

    def test_reads_top_level_txt(self, tmp_path):
        from zmux.zpip import _discover_modules
        staging = self._wheel(tmp_path, {
            "markdown_it_py-4.0.dist-info/top_level.txt": "markdown_it\n",
            "markdown_it/__init__.py": "",
        })
        assert _discover_modules(staging, "markdown-it-py")[0] == "markdown_it"

    def test_falls_back_to_the_extracted_tree(self, tmp_path):
        from zmux.zpip import _discover_modules
        staging = self._wheel(tmp_path, {
            "python_dateutil-2.9.dist-info/METADATA": "Name: python-dateutil",
            "dateutil/__init__.py": "",
        })
        assert _discover_modules(staging, "python-dateutil")[0] == "dateutil"

    def test_prefers_the_name_matching_the_distribution(self, tmp_path):
        from zmux.zpip import _discover_modules
        staging = self._wheel(tmp_path, {
            "attrs-25.0.dist-info/METADATA": "Name: attrs",
            "attr/__init__.py": "", "attrs/__init__.py": "",
        })
        assert _discover_modules(staging, "attrs")[0] == "attrs"

    def test_single_file_module(self, tmp_path):
        from zmux.zpip import _discover_modules
        staging = self._wheel(tmp_path, {
            "six-1.17.dist-info/METADATA": "Name: six", "six.py": "",
        })
        assert _discover_modules(staging, "six") == ["six"]

    def test_metadata_dirs_are_never_returned(self, tmp_path):
        from zmux.zpip import _discover_modules
        staging = self._wheel(tmp_path, {
            "pkg-1.0.dist-info/RECORD": "", "pkg-1.0.data/scripts/tool": "", "pkg/__init__.py": "",
        })
        modules = _discover_modules(staging, "pkg")
        assert modules == ["pkg"]
        assert not any(m.endswith(("dist-info", "data")) for m in modules)

    def test_empty_wheel_falls_back_to_the_guess(self, tmp_path):
        from zmux.zpip import _discover_modules
        staging = tmp_path / "empty"
        staging.mkdir()
        assert _discover_modules(staging, "mystery-pkg") == ["mystery_pkg"]


class TestStorageSetup:
    """`zmux-setup-storage` — opt-in shared storage, honest when unavailable."""

    def test_setup_creates_the_storage_directory(self):
        from zmux import storage
        result = storage.setup()
        assert Path(result["storage_dir"]).is_dir()

    def test_setup_never_raises_off_android(self):
        from zmux import storage
        assert storage.setup()["android"] is False

    def test_permission_request_is_a_noop_off_android(self):
        from zmux import storage
        granted, message = storage.request_permissions()
        assert granted is True and "Android" in message

    def test_output_is_honest_when_nothing_is_linked(self):
        from zmux import storage
        text = storage.format_setup({
            "ok": False, "granted": True, "message": "x",
            "storage_dir": "/tmp/s", "links": [], "android": True,
        })
        assert "No shared directories were reachable" in text
        assert "scoped storage" in text

    def test_output_lists_links_when_present(self):
        from zmux import storage
        text = storage.format_setup({
            "ok": True, "granted": True, "message": "granted",
            "storage_dir": "/tmp/s", "android": True,
            "links": [("downloads", "/sdcard/Download", "ok")],
        })
        assert "~/storage/downloads" in text and "/sdcard/Download" in text

    def test_command_is_reachable_from_the_shell(self, tmp_path):
        result = PythonShell(tmp_path).execute("zmux-setup-storage")
        assert "ZMUX storage setup" in result["stdout"]

    def test_command_is_listed_in_help(self):
        from zmux.terminal import HELP_TEXT
        assert "zmux-setup-storage" in HELP_TEXT


class TestStoragePermissionBridge:
    """`zmux-setup-storage` must not crash when the Java bridge is degraded.

    On-device regression: p4a's android.permissions module ran autoclass()
    from the command-executor worker thread, where JNI FindClass falls back
    to the system class loader and raises ClassNotFoundException for
    org.kivy.android.PythonActivity. The fix routes the request through the
    primed javabridge and degrades to a clear message when it is absent.
    """

    def test_request_uses_primed_activity_directly(self, monkeypatch):
        from zmux import storage
        calls = []

        class FakeActivity:
            def requestPermissions(self, permissions):
                calls.append(list(permissions))

        monkeypatch.setattr(storage, "_is_android", lambda: True)
        monkeypatch.setattr("zmux.javabridge.mActivity", lambda: FakeActivity())
        granted, message = storage.request_permissions()
        assert granted is True
        assert any("READ_EXTERNAL_STORAGE" in p for p in calls[0])
        assert any("WRITE_EXTERNAL_STORAGE" in p for p in calls[0])

    def test_request_falls_back_gracefully_without_bridge(self, monkeypatch):
        from zmux import storage
        monkeypatch.setattr(storage, "_is_android", lambda: True)
        monkeypatch.setattr("zmux.javabridge.mActivity", lambda: None)
        granted, message = storage.request_permissions()
        assert granted is False
        assert "grant" in message.lower()

    def test_request_never_crashes_on_java_error(self, monkeypatch):
        from zmux import storage

        def boom(_permissions):
            raise RuntimeError("simulated JVM exception")

        class BrokenActivity:
            def requestPermissions(self, permissions):
                boom(permissions)

        monkeypatch.setattr(storage, "_is_android", lambda: True)
        monkeypatch.setattr("zmux.javabridge.mActivity", lambda: BrokenActivity())
        granted, message = storage.request_permissions()
        assert granted is False
        assert "simulated JVM exception" in message
