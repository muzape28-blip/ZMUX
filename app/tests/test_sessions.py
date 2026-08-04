"""Tests for multi-session support (zmux.sessions.SessionManager)."""

import time

import pytest

from zmux.sessions import SessionManager, reset_manager


class _FakeWS:
    def __init__(self):
        self.data = bytearray()
        self.callbacks = {}

    def register_callbacks(self, on_data=None, on_resize=None):
        self.callbacks = {"on_data": on_data, "on_resize": on_resize}

    def broadcast(self, payload):
        self.data.extend(payload)


def _wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


@pytest.fixture
def manager():
    mgr = SessionManager(_FakeWS())
    mgr.create()
    yield mgr
    mgr.stop_all()
    reset_manager()


class TestLifecycle:
    def test_first_session_is_active(self, manager):
        assert len(manager.ids()) == 1
        assert manager.active_id == manager.ids()[0]
        assert manager.active is not None

    def test_create_returns_distinct_ids(self, manager):
        second = manager.create()
        assert second != manager.ids()[0]
        assert len(manager.ids()) == 2

    def test_create_activates_the_new_session(self, manager):
        second = manager.create()
        assert manager.active_id == second

    def test_create_without_activate_keeps_current(self, manager):
        first = manager.active_id
        manager.create(activate=False)
        assert manager.active_id == first

    def test_session_cap_is_enforced(self, manager):
        while len(manager.ids()) < SessionManager.MAX_SESSIONS:
            manager.create()
        with pytest.raises(ValueError, match="session limit"):
            manager.create()

    def test_switch_to_unknown_id_is_false(self, manager):
        assert manager.switch("nope") is False

    def test_close_unknown_id_is_false(self, manager):
        assert manager.close("nope") is False


class TestIsolation:
    def test_sessions_have_independent_python_globals(self, manager):
        first = manager.active_id
        manager.active.shell.globals["marker"] = "from_first"
        second = manager.create()
        assert "marker" not in manager.get(second).shell.globals
        manager.switch(first)
        assert manager.active.shell.globals["marker"] == "from_first"

    def test_sessions_have_independent_working_directories(self, manager, tmp_path):
        first_cwd = manager.active.shell.cwd
        second = manager.create()
        (tmp_path / "sub").mkdir()
        manager.get(second).shell.cwd = tmp_path
        assert manager.get(second).shell.cwd != first_cwd

    def test_background_session_output_does_not_reach_websocket(self, manager):
        background = manager.ids()[0]
        manager.create()  # becomes active
        manager.ws_server.data.clear()
        manager.get(background)._emit(b"background_noise")
        assert b"background_noise" not in manager.ws_server.data

    def test_background_session_still_records_scrollback(self, manager):
        background = manager.ids()[0]
        manager.create()
        manager.get(background)._emit(b"recorded_offscreen")
        assert b"recorded_offscreen" in manager.get(background).get_scrollback()

    def test_active_session_output_reaches_websocket(self, manager):
        manager.ws_server.data.clear()
        manager.active._emit(b"visible_now")
        assert b"visible_now" in manager.ws_server.data


class TestSwitching:
    def test_switch_replays_target_scrollback(self, manager):
        first = manager.ids()[0]
        manager.get(first)._emit(b"first_session_history")
        manager.create()
        manager.ws_server.data.clear()
        manager.switch(first)
        assert b"first_session_history" in manager.ws_server.data

    def test_switch_clears_screen_before_replay(self, manager):
        first = manager.ids()[0]
        manager.create()
        manager.ws_server.data.clear()
        manager.switch(first)
        assert manager.ws_server.data.startswith(b"\x1b[?1049l\x1b[?47l\x1b[?1047l\x1b[2J\x1b[H")

    def test_input_is_routed_to_the_active_session(self, manager):
        first = manager.ids()[0]
        second = manager.create()
        manager.write_input(b"marker_text")
        assert b"marker_text" in manager.get(second).get_scrollback()
        assert b"marker_text" not in manager.get(first).get_scrollback()


class TestClosing:
    def test_closing_active_activates_a_neighbour(self, manager):
        first = manager.ids()[0]
        second = manager.create()
        manager.close(second)
        assert manager.active_id == first
        assert second not in manager.ids()

    def test_closing_the_last_session_opens_a_fresh_one(self, manager):
        only = manager.ids()[0]
        manager.close(only)
        assert len(manager.ids()) == 1, "user must never be left with zero terminals"
        assert manager.active_id is not None
        assert manager.ids()[0] != only

    def test_closing_a_background_session_keeps_active_unchanged(self, manager):
        background = manager.ids()[0]
        active = manager.create()
        manager.close(background)
        assert manager.active_id == active

    def test_closed_session_is_stopped(self, manager):
        second = manager.create()
        session = manager.get(second)
        manager.close(second)
        assert session.is_running is False


class TestSnapshot:
    def test_snapshot_reports_order_and_active(self, manager):
        first = manager.ids()[0]
        second = manager.create()
        snap = manager.snapshot()
        assert [s["id"] for s in snap["sessions"]] == [first, second]
        assert snap["active"] == second
        assert snap["max"] == SessionManager.MAX_SESSIONS

    def test_resize_defers_background_tui_until_tab_is_visible(self, manager):
        first = manager.ids()[0]
        second = manager.create()  # second is active
        manager.resize(120, 40)
        assert manager.get(second).shell.width == 120
        # The inactive tab is deliberately left alone during IME animation.
        assert manager.get(first).shell.width != 120
        manager.switch(first)
        assert manager.get(first).shell.width == 120


class TestEndToEnd:
    def test_command_in_one_session_does_not_disturb_the_other(self, manager):
        first = manager.ids()[0]
        second = manager.create()
        manager.switch(first)
        manager.write_input(b"echo alpha_marker\n")
        assert _wait_for(lambda: b"alpha_marker" in manager.get(first).get_scrollback())
        assert b"alpha_marker" not in manager.get(second).get_scrollback()

        manager.switch(second)
        manager.write_input(b"echo beta_marker\n")
        assert _wait_for(lambda: b"beta_marker" in manager.get(second).get_scrollback())
        assert b"beta_marker" not in manager.get(first).get_scrollback()


class TestWebSocketProtocol:
    """Session control messages over a real websocket connection."""

    @staticmethod
    def _connect(port):
        import base64, socket
        from zmux.security import AUTH_TOKEN
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", port))
        sock.sendall(
            f"GET /?token={AUTH_TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(4096)
        assert b"101" in buf, buf[:80]
        return sock

    @staticmethod
    def _send_text(sock, text):
        import os, struct
        payload = text.encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytes([0x81])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        else:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        sock.sendall(header + mask + masked)

    def test_new_and_close_session_over_websocket(self):
        from zmux.sessions import get_manager, reset_manager
        from zmux.ws_server import WebSocketServer

        reset_manager()
        server = WebSocketServer(host="127.0.0.1", port=0)
        server.start()
        time.sleep(0.2)
        port = server.server_socket.getsockname()[1]
        sock = None
        try:
            manager = get_manager(server)
            server.register_callbacks(on_data=manager.write_input, on_resize=manager.resize)
            assert len(manager.ids()) == 1

            sock = self._connect(port)
            self._send_text(sock, '{"action":"session.new"}')
            assert _wait_for(lambda: len(manager.ids()) == 2), "session.new ignored"

            newest = manager.ids()[-1]
            assert manager.active_id == newest

            self._send_text(sock, '{"action":"session.close","id":"%s"}' % newest)
            assert _wait_for(lambda: len(manager.ids()) == 1), "session.close ignored"
        finally:
            if sock:
                sock.close()
            server.stop()
            reset_manager()

    def test_resize_still_works_alongside_session_actions(self):
        from zmux.sessions import get_manager, reset_manager
        from zmux.ws_server import WebSocketServer

        reset_manager()
        server = WebSocketServer(host="127.0.0.1", port=0)
        server.start()
        time.sleep(0.2)
        port = server.server_socket.getsockname()[1]
        sock = None
        try:
            manager = get_manager(server)
            server.register_callbacks(on_data=manager.write_input, on_resize=manager.resize)
            sock = self._connect(port)
            self._send_text(sock, '{"action":"resize","cols":133,"rows":42}')
            assert _wait_for(lambda: manager.active.shell.width == 133)
        finally:
            if sock:
                sock.close()
            server.stop()
            reset_manager()


class TestCallbackOwnership:
    """A managed session must never hijack the websocket's input callbacks.

    Regression: ownership was detected by comparing a stored bound method
    with `ws.broadcast` using `is`, which is always False (each attribute
    access creates a new bound-method object). Standalone sessions therefore
    silently failed to register their callbacks.
    """

    def test_standalone_session_registers_its_callbacks(self):
        from zmux.pty_session import PTYTerminalSession
        ws = _FakeWS()
        session = PTYTerminalSession(ws)
        session.start()
        try:
            assert ws.callbacks.get("on_data") is not None, "standalone session must register"
            assert ws.callbacks["on_data"] == session.write_input
        finally:
            session.stop()

    def test_managed_session_does_not_register(self, manager):
        ws = manager.ws_server
        ws.callbacks.clear()
        manager.create()
        assert ws.callbacks == {}, "managed session stole the websocket callbacks"


class TestRepaint:
    def test_create_repaints_screen_when_activating(self):
        """A new tab must be a new *page*: clear + replay, not append."""
        ws = _FakeWS()
        mgr = SessionManager(ws)
        first = mgr.create()
        ws.data.clear()
        second = mgr.create()
        try:
            assert mgr.active_id == second
            assert b"\x1b[2J\x1b[H" in ws.data, "screen must be cleared"
            # The banner of the *second* session follows the clear.
            assert ws.data.index(b"\x1b[2J\x1b[H") < ws.data.index(b"zmux:")
        finally:
            mgr.stop_all()
            reset_manager()

    def test_switch_leaves_alternate_screen_before_replay(self):
        """Switching away from Vim/less must not leave the next tab inside
        xterm's alternate buffer."""
        from zmux.sessions import RESET_TERMINAL_SCREEN
        ws = _FakeWS()
        mgr = SessionManager(ws)
        first = mgr.create()
        second = mgr.create(activate=False)
        try:
            ws.data.clear()
            mgr.switch(second)
            assert ws.data.startswith(RESET_TERMINAL_SCREEN)
            assert b"\x1b[?1049l" in ws.data
        finally:
            mgr.stop_all()
            reset_manager()

    def test_first_create_does_not_need_clear(self):
        ws = _FakeWS()
        mgr = SessionManager(ws)
        mgr.create()
        try:
            assert b"\x1b[2J\x1b[H" not in ws.data
        finally:
            mgr.stop_all()
            reset_manager()
