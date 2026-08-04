"""Session-level tests for the real Alpine PTY shell mode (Phase 1 + 2).

These wire the real PTY machinery into :class:`PTYTerminalSession` through a
*fake proot* (a script that strips proot flags and execs the guest command):
the shell that runs inside the PTY is a real /bin/sh, so the tests exercise
the actual fork/setsid/TIOCSCTTY path, input passthrough, resize, exit and
detach — nothing is mocked except the proot binary itself.
"""

import os
import time

import pytest

from zmux import linuxenv
from zmux.pty_session import PTYTerminalSession


class _FakeWS:
    """Minimal ws_server stand-in (no sockets), same shape as the other suite."""

    def __init__(self):
        self.data = bytearray()
        self.callbacks = {}

    def register_callbacks(self, on_data, on_resize):
        self.callbacks = {"on_data": on_data, "on_resize": on_resize}

    def broadcast(self, payload):
        self.data.extend(payload)


def _wait_for(predicate, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


#: Fake proot: drop every proot flag (-0, -r, -w, -b, --kill-on-exit, ...)
#: and exec the first non-flag arguments (the guest command).
FAKE_PROOT = """#!/bin/sh
out=""
skip=0
for arg in "$@"; do
  if [ "$skip" -gt 0 ]; then skip=0; continue; fi
  case "$arg" in
    -0|--kill-on-exit|--link2symlink|--sysvipc) continue ;;
    -r|-w|-b) skip=1; continue ;;
    *) out="$out $arg" ;;
  esac
done
# shellcheck disable=SC2086
exec $out
"""


@pytest.fixture
def alpine_env(monkeypatch, tmp_path):
    """Minimal installed rootfs + executable fake proot + writable guest home."""
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "usr" / "local" / "bin").mkdir(parents=True)
    (rootfs / "bin" / "busybox").write_text("")
    (rootfs / "etc" / "alpine-release").write_text(linuxenv.ALPINE_VERSION)
    fake = tmp_path / "proot"
    fake.write_text(FAKE_PROOT)
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(linuxenv, "_ROOTFS_DIR", rootfs)
    monkeypatch.setenv("ZMUX_PROOT_BIN", str(fake))
    monkeypatch.setattr(linuxenv, "GUEST_HOME", str(home))
    return rootfs


@pytest.fixture
def session():
    s = PTYTerminalSession(_FakeWS())
    s.start()
    yield s
    s.stop()


def test_bare_linux_without_alpine_is_honest(session):
    """No rootfs -> `linux` explains how to install, stays on host console."""
    session.write_input(b"linux\n")
    assert _wait_for(lambda: b"not installed" in session.get_scrollback())
    assert _wait_for(lambda: b"linux-setup" in session.get_scrollback())
    # Still on the host console, prompt rendered.
    assert _wait_for(lambda: b"zmux:" in session.get_scrollback())
    assert session.pty is None


def test_bare_linux_enters_real_pty_shell_and_exit_returns(session, alpine_env):
    session.write_input(b"linux\n")
    assert _wait_for(lambda: b"[ZMUX Alpine Linux - real PTY]" in session.get_scrollback())
    assert session.pty is not None

    session.write_input(b"echo HI-FROM-ALPINE\n")
    assert _wait_for(lambda: b"HI-FROM-ALPINE" in session.get_scrollback())

    old_pty = session.pty
    session.write_input(b"exit\n")
    assert _wait_for(lambda: b"[Alpine shell exited" in session.get_scrollback())
    # Alpine-first ZMUX never falls back to the embedded Python console after
    # `exit`; it presents a fresh real shell in the same terminal tab.
    assert _wait_for(lambda: session.pty is not None and session.pty is not old_pty)


def test_input_is_passthrough_no_double_echo(session, alpine_env):
    """In PTY mode the shell + kernel echo the line exactly once; ZMUX must
    not echo a second copy (the old line discipline would have)."""
    session.write_input(b"linux\n")
    assert _wait_for(lambda: session.pty is not None)
    session.write_input(b"echo XYZ\r")
    # Wait until both occurrences arrived (echoed command line + output),
    # then assert exactly 2 — a double-echoing bridge would reach 3.
    assert _wait_for(lambda: session.get_scrollback().count(b"XYZ") >= 2)
    assert session.get_scrollback().count(b"XYZ") == 2, session.get_scrollback()


def test_resize_reaches_pty_shell(session, alpine_env):
    session.write_input(b"linux\n")
    assert _wait_for(lambda: session.pty is not None)
    session.resize(47, 11)
    session.write_input(b"stty size\r")
    assert _wait_for(lambda: b"11 47" in session.get_scrollback())


def test_toggle_pty_detaches_and_reattaches(session, alpine_env):
    session.write_input(b"linux\n")
    assert _wait_for(lambda: session.pty is not None)
    session.toggle_pty()
    assert _wait_for(lambda: b"detached from Alpine shell" in session.get_scrollback())
    assert session.pty is None
    assert _wait_for(lambda: b"zmux:" in session.get_scrollback())
    # Toggle again re-enters the Alpine shell.
    session.toggle_pty()
    assert _wait_for(lambda: b"[ZMUX Alpine Linux - real PTY]" in session.get_scrollback())
    assert session.pty is not None


def test_detach_then_reenter_old_exit_does_not_clobber_new(session, alpine_env):
    """Race guard: the old pty's reader calling on_exit after a quick
    detach+re-entry must not clear the brand-new pty."""
    session.write_input(b"linux\n")
    assert _wait_for(lambda: session.pty is not None)
    old = session.pty
    session.toggle_pty()                      # detach -> kills old pty
    assert _wait_for(lambda: session.pty is None)
    session.toggle_pty()                      # immediately re-enter
    assert _wait_for(lambda: session.pty is not None and session.pty is not old)
    new = session.pty
    # Let the old reader finish reaping and fire its on_exit...
    assert old.wait_exited(timeout=8.0) is not None
    time.sleep(0.5)
    # ...the new pty must still own the session and keep answering.
    assert session.pty is new
    session.write_input(b"echo STILL-MINE\r")
    assert _wait_for(lambda: b"STILL-MINE" in session.get_scrollback())


def test_stop_kills_pty_child(session, alpine_env):
    session.write_input(b"linux\n")
    assert _wait_for(lambda: session.pty is not None)
    pty = session.pty
    session.stop()
    assert session.pty is None
    assert pty.wait_exited(timeout=8.0) is not None  # reaped


def test_phase2_auto_start_alpine_shell(alpine_env):
    """Phase 2: with rootfs + proot present, a fresh session boots straight
    into the Alpine PTY shell (no `linux` needed)."""
    s = PTYTerminalSession(_FakeWS())
    try:
        s.start()
        assert _wait_for(lambda: b"[ZMUX Alpine Linux - real PTY]" in s.get_scrollback())
        assert s.pty is not None
        # The shell actually answers.
        s.write_input(b"echo AUTO-OK\n")
        assert _wait_for(lambda: b"AUTO-OK" in s.get_scrollback())
    finally:
        s.stop()


def test_alpine_is_not_overridden_by_legacy_host_shell_environment(alpine_env, monkeypatch):
    """The old host-console switch is intentionally ignored: ZMUX exposes
    one shell contract to users, Alpine in a real PTY."""
    monkeypatch.setenv("ZMUX_SHELL_START", "zmux")
    s = PTYTerminalSession(_FakeWS())
    try:
        s.start()
        assert _wait_for(lambda: b"[ZMUX Alpine Linux - real PTY]" in s.get_scrollback())
        assert s.pty is not None
    finally:
        s.stop()


def test_guest_wrappers_are_installed_into_rootfs(session, alpine_env):
    session.write_input(b"linux\n")
    assert _wait_for(lambda: session.pty is not None)
    wrapper = alpine_env / "usr" / "local" / "bin" / "gates"
    assert wrapper.is_file()
    assert os.access(wrapper, os.X_OK)
    text = wrapper.read_text(encoding="utf-8")
    assert "app diagnostic" in text
    storage_wrapper = alpine_env / "usr" / "local" / "bin" / "zmux-setup-storage"
    assert storage_wrapper.is_file()
    assert "zmux-setup-storage" in storage_wrapper.read_text(encoding="utf-8")


def test_linux_dashdash_help_still_shows_help(session, alpine_env):
    """`linux --help` (with args) is not swallowed by the PTY interception."""
    session.write_input(b"linux --help\n")
    assert _wait_for(lambda: b"linux <command...>" in session.get_scrollback())
    assert session.pty is None
