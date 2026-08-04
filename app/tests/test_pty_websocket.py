"""
Tests for ZMUX PTY and WebSocket Server Integration.

Verifies secure handshake authentication, dynamic port binding,
WebSocket framing, and PTY process spawn capabilities.
"""

import socket
import threading
import time

import pytest

from zmux.security import AUTH_TOKEN
from zmux.ws_server import WebSocketServer
from zmux.pty_session import PTYTerminalSession, get_pty_session


def _read_http_response(sock, timeout=5.0):
    """Read exactly the HTTP response head and decode only that.

    The server sends the 101 handshake and then, immediately, the active
    session's scrollback as a *binary* WebSocket frame. A single
    ``recv(2048).decode("utf-8")`` can therefore capture both and blow up with
    ``UnicodeDecodeError: invalid start byte`` on 0x82 — the binary frame
    header. That is what made these tests fail intermittently in CI: whether
    the two writes coalesce into one TCP segment depends on timing and on
    whether a previous test left scrollback behind.

    Reading up to (and only up to) the CRLFCRLF header terminator makes the
    tests independent of that race.
    """
    sock.settimeout(timeout)
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(1)
        if not chunk:
            break
        buffer += chunk
    return buffer.decode("utf-8", errors="replace")


def test_websocket_server_handshake():
    """Test that unauthorized token is rejected with 401, and valid token gets 101 Switching Protocols."""
    # Find an open port by binding port=0
    server = WebSocketServer(host="127.0.0.1", port=0)
    server.start()

    # Wait briefly for start
    time.sleep(0.1)
    assert server.is_running
    port = server.server_socket.getsockname()[1]

    try:
        # 1. Connection with invalid token
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", port))

        bad_handshake = (
            "GET /?token=invalid_zaba_token HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(bad_handshake.encode("utf-8"))

        response = _read_http_response(sock)
        assert "401 Unauthorized" in response
        sock.close()

        # 2. Connection with valid token
        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.connect(("127.0.0.1", port))

        good_handshake = (
            f"GET /?token={AUTH_TOKEN} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock2.sendall(good_handshake.encode("utf-8"))

        response2 = _read_http_response(sock2)
        assert "101 Switching Protocols" in response2
        assert "Sec-WebSocket-Accept" in response2
        sock2.close()

    finally:
        server.stop()


def test_pty_session_lifecycle():
    """Test PTY session startup, input writing, and graceful stopping."""
    server = WebSocketServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)

    try:
        session = PTYTerminalSession(server)
        session.start()

        assert session.is_running
        # Python-native mode deliberately does not spawn a shell process.
        assert session.process is None

        # Try to write some characters
        session.write_input(b"echo pty_test\n")
        time.sleep(0.2)

        # Output should be generated and buffered
        scrollback = session.get_scrollback()
        assert len(scrollback) > 0

        session.stop()
        assert not session.is_running
        assert session.process is None
    finally:
        server.stop()


def test_global_pty_session_singleton():
    """Test that get_pty_session correctly returns a singleton instance."""
    server = WebSocketServer(host="127.0.0.1", port=0)
    try:
        s1 = get_pty_session(server)
        s2 = get_pty_session(server)
        assert s1 is s2
    finally:
        server.stop()


def test_broadcast_with_dead_client_does_not_deadlock():
    """Regression: a dead-but-still-registered client (WebView reload/rotation
    race) must not deadlock broadcast(); previously _unregister_client() was
    called while already holding clients_lock, freezing the PTY reader thread,
    every later broadcast, and stop() — the app-wide "stuck" state."""
    server = WebSocketServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)

    dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with server.clients_lock:
        server.clients.add(dead)
    dead.close()  # sendall() now raises immediately

    finished = threading.Event()

    def _broadcast():
        server.broadcast(b"pty-output")
        finished.set()

    t = threading.Thread(target=_broadcast, daemon=True)
    t.start()

    assert finished.wait(timeout=5.0), "broadcast() deadlocked on dead client"

    # Dead client must have been removed, and the lock must still be usable
    with server.clients_lock:
        assert dead not in server.clients
    server.stop()


def test_websocket_start_with_prebound_listener():
    """run_server() hands a live listener to WebSocketServer to avoid the
    probe-then-bind race; the server must serve on exactly that socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    server = WebSocketServer(host="127.0.0.1", port=port)
    server.start(listener=listener)
    time.sleep(0.1)
    try:
        assert server.is_running
        assert server.server_socket is listener

        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall((
            f"GET /?token={AUTH_TOKEN} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("utf-8"))
        response = _read_http_response(sock)
        assert "101 Switching Protocols" in response
        sock.close()
    finally:
        server.stop()


def test_websocket_start_with_multiple_listeners():
    """Verify WebSocketServer can listen on multiple bound sockets simultaneously (e.g. dual IPv4/IPv6)."""
    l1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    l1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    l1.bind(("127.0.0.1", 0))
    l1.listen(5)
    port1 = l1.getsockname()[1]

    l2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    l2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    l2.bind(("127.0.0.1", 0))
    l2.listen(5)
    port2 = l2.getsockname()[1]

    server = WebSocketServer(host="127.0.0.1", port=port1)
    server.start(listeners=[l1, l2])
    time.sleep(0.1)
    try:
        assert server.is_running
        for p in (port1, port2):
            sock = socket.create_connection(("127.0.0.1", p))
            sock.sendall((
                f"GET /?token={AUTH_TOKEN} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("utf-8"))
            response = _read_http_response(sock)
            assert "101 Switching Protocols" in response
            sock.close()
    finally:
        server.stop()


def test_python_native_session_does_not_depend_on_openpty(monkeypatch):
    """Python-native mode must work even where Android denies /dev/ptmx."""
    import os

    def _mock_openpty():
        raise PermissionError("[Errno 13] Permission denied: '/dev/ptmx'")

    monkeypatch.setattr(os, "openpty", _mock_openpty, raising=False)

    server = WebSocketServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)

    try:
        session = PTYTerminalSession(server)
        session.start()

        assert session.is_running
        assert session.process is None

        session.write_input(b"echo python_native_ok\n")
        time.sleep(0.2)
        scrollback = session.get_scrollback()
        assert b"python_native_ok" in scrollback

        session.stop()
        assert not session.is_running
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Interactive terminal behaviour (PR 2): mode split, kill switch, stdin,
# history — exercised without any real sockets via a ws_server stub.
# ---------------------------------------------------------------------------


class _FakeWS:
    """Minimal ws_server stand-in (no sockets)."""

    def __init__(self):
        self.data = bytearray()
        self.callbacks = {}
        #: (monotonic timestamp, payload) per broadcast, so tests can assert
        #: *when* output reached the client, not just that it eventually did.
        self.events = []

    def register_callbacks(self, on_data, on_resize):
        self.callbacks = {"on_data": on_data, "on_resize": on_resize}

    def broadcast(self, payload):
        self.data.extend(payload)
        self.events.append((time.monotonic(), bytes(payload)))


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def interactive():
    session = PTYTerminalSession(_FakeWS())
    session.start()
    yield session
    session.stop()


class TestModeSplit:
    def test_shell_is_the_default_mode_and_python_enters_repl(self, interactive):
        assert _wait_for(lambda: b"zmux:" in interactive.get_scrollback())

        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())

        interactive.write_input(b"21+21\n")
        assert _wait_for(lambda: b"42\r\n" in interactive.get_scrollback())

    def test_repl_is_pure_python_not_shell(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        # Inside the REPL, shell builtins must NOT be intercepted.
        interactive.write_input(b"ls\n")
        assert _wait_for(lambda: b"NameError" in interactive.get_scrollback())

    def test_exit_function_returns_to_shell(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        interactive.write_input(b"exit()\n")
        # NB: no rstrip() here — the prompt itself ends with a space.
        assert _wait_for(lambda: interactive.get_scrollback().endswith(b"$ "))

    def test_python_with_arguments_runs_script_not_repl(self, interactive, tmp_path):
        """`python file.py` must execute the script — only the bare word
        `python` enters the REPL (demo-caught regression)."""
        script = tmp_path / "mini.py"
        script.write_text("print('script_output_marker')\n", encoding="utf-8")
        interactive.write_input(f"python {script}\n".encode("utf-8"))
        assert _wait_for(lambda: b"script_output_marker" in interactive.get_scrollback())
        # Still in shell mode: no REPL prompt was opened.
        assert interactive.get_scrollback().rstrip().rstrip(b"\r\n").endswith(b"$")

    def test_compound_block_and_blank_line_close(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        interactive.write_input(b"for i in range(3):\n")
        assert _wait_for(lambda: b"... " in interactive.get_scrollback())
        interactive.write_input(b" print(i*10)\n")
        time.sleep(0.2)
        interactive.write_input(b"\n")  # blank line closes the block (REPL semantics)
        assert _wait_for(lambda: b"0\r\n10\r\n20\r\n" in interactive.get_scrollback())


class TestKillSwitch:
    def test_ctrl_c_stops_runaway_python(self, interactive):
        interactive.write_input(b"while True: pass\n")
        assert _wait_for(lambda: interactive._busy.is_set()), "command never started"
        interactive.write_input(b"\x03")
        assert _wait_for(lambda: b"KeyboardInterrupt" in interactive.get_scrollback(), timeout=15)
        # The session must still accept and run the next command.
        interactive.write_input(b"echo still_alive\n")
        assert _wait_for(lambda: b"still_alive\r\n" in interactive.get_scrollback())

    def test_ctrl_c_kills_subprocess_pipeline(self, interactive):
        interactive.write_input(b"sleep 30\n")
        assert _wait_for(lambda: interactive._busy.is_set()), "sleep never started"
        start = time.monotonic()
        interactive.write_input(b"\x03")
        # Which marker appears depends on ^C timing vs. process spawn — both
        # are correct "interrupted" renderings (like a real terminal): the
        # signal hint when the process died by SIGINT, KeyboardInterrupt when
        # the cancellation landed before/around spawn.
        assert _wait_for(
            lambda: b"signal" in interactive.get_scrollback()
            or b"KeyboardInterrupt" in interactive.get_scrollback(),
            timeout=15,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 15, f"interrupt took too long ({elapsed:.1f}s) — sleep 30 not killed"
        interactive.write_input(b"echo after_kill\n")
        assert _wait_for(lambda: b"after_kill\r\n" in interactive.get_scrollback())


class TestStdinAndHistory:
    def test_input_reads_queued_stdin_line(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        interactive.write_input(b"name = input('who? ')\n")
        assert _wait_for(lambda: interactive._busy.is_set())
        interactive.write_input(b"zaba\n")  # busy -> routed to stdin queue
        assert _wait_for(lambda: not interactive._busy.is_set()), "input() never returned"
        interactive.write_input(b"name\n")
        assert _wait_for(lambda: b"'zaba'" in interactive.get_scrollback())

    def test_arrow_up_recalls_last_command(self, interactive):
        interactive.write_input(b"echo hist_marker\n")
        assert _wait_for(lambda: b"hist_marker\r\n" in interactive.get_scrollback())
        before = interactive.get_scrollback().count(b"hist_marker\r\n")
        interactive.write_input(b"\x1b[A")  # Up
        time.sleep(0.1)
        interactive.write_input(b"\n")
        assert _wait_for(
            lambda: interactive.get_scrollback().count(b"hist_marker\r\n") >= before + 1
        )

    def test_escape_sequences_do_not_leak_into_line_buffer(self, interactive):
        # Pressing Up with empty history must not type "[A" (previous bug).
        interactive.write_input(b"\x1b[A")
        time.sleep(0.1)
        interactive.write_input(b"echo clean_line\n")
        assert _wait_for(lambda: b"clean_line\r\n" in interactive.get_scrollback())

class TestStreamingOutput:
    """Output must reach the terminal while a command runs, not after it ends.

    Regression tests for the batched-output bug: results were captured into a
    StringIO and emitted only once execute() returned, so progressive commands
    looked frozen and input() prompts were invisible until after the answer.
    """

    def test_progressive_output_arrives_during_command(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        interactive.ws_server.events.clear()
        interactive.write_input(b"import time\n")
        interactive.write_input(b"for i in range(3):\n    print('tick', i); time.sleep(0.5)\n\n")
        assert _wait_for(lambda: b"tick 2" in interactive.get_scrollback(), timeout=20)

        ticks = [ts for ts, payload in interactive.ws_server.events if b"tick" in payload]
        assert len(ticks) >= 2, f"ticks were coalesced into one write: {ticks}"
        # Three prints separated by 0.5s must span ~1s of wall clock. Batched
        # output would deliver them in a single burst (spread ~0).
        assert ticks[-1] - ticks[0] > 0.7, (
            f"output was batched: all ticks within {ticks[-1] - ticks[0]:.2f}s"
        )

    def test_input_prompt_is_visible_before_stdin_is_answered(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        interactive.ws_server.events.clear()
        interactive.write_input(b"answer = input('Your name? ')\n")
        assert _wait_for(lambda: interactive._busy.is_set()), "input() never blocked"
        # The prompt has no trailing newline, so only an explicit flush before
        # the blocking read can put it on screen.
        assert _wait_for(
            lambda: b"Your name?" in b"".join(p for _, p in interactive.ws_server.events)
        ), "prompt was not shown while waiting for input"
        assert interactive._busy.is_set(), "should still be blocked on stdin"

        interactive.write_input(b"Zaba\n")
        assert _wait_for(lambda: not interactive._busy.is_set()), "input() never returned"
        interactive.write_input(b"answer\n")
        assert _wait_for(lambda: b"'Zaba'" in interactive.get_scrollback())

    def test_builtin_command_output_is_rendered_exactly_once(self, interactive):
        """Built-ins bypass the sink; they must still render, and not twice.

        The scrollback holds the keystroke echo of the typed line *and* the
        command's output, so the marker legitimately appears twice; the
        output line itself ("marker" followed by CRLF at line start) once.
        """
        interactive.write_input(b"echo dedupe_marker\n")
        # Wait for the *output* line, not the keystroke echo that precedes it.
        assert _wait_for(lambda: b"\r\ndedupe_marker\r\n" in interactive.get_scrollback())
        assert _wait_for(lambda: interactive.get_scrollback().endswith(b"$ "))
        assert interactive.get_scrollback().count(b"\r\ndedupe_marker\r\n") == 1

    def test_streamed_python_output_is_not_duplicated(self, interactive):
        interactive.write_input(b"python\n")
        assert _wait_for(lambda: b">>> " in interactive.get_scrollback())
        # Split literal so the typed-line echo cannot match the printed value.
        interactive.write_input(b"print('once' + '_only')\n")
        assert _wait_for(lambda: b"once_only" in interactive.get_scrollback())
        assert interactive.get_scrollback().count(b"once_only") == 1


def test_handshake_read_is_not_corrupted_by_trailing_scrollback():
    """Regression: the 101 head must be readable when a binary frame follows.

    The server replays the active session's scrollback as a binary WebSocket
    frame immediately after the handshake. When both land in one TCP segment,
    decoding the whole read as UTF-8 raised
    `UnicodeDecodeError: invalid start byte` on 0x82 (the frame header) —
    an intermittent CI failure that depended purely on timing and on whether
    an earlier test had left scrollback behind.
    """
    import socket as _socket
    from zmux.sessions import get_manager, reset_manager

    reset_manager()
    listener = _socket.socket()
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    server = WebSocketServer(host="127.0.0.1", port=port)
    server.start(listeners=[listener])
    time.sleep(0.2)
    client = None
    try:
        # Guarantee there IS scrollback to replay, so the race is forced
        # rather than waited for.
        get_manager(server).active._emit(b"y" * 400)
        time.sleep(0.2)

        client = _socket.create_connection(("127.0.0.1", port))
        client.sendall((
            f"GET /?token={AUTH_TOKEN} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("utf-8"))
        # Let the handshake and the binary frame coalesce before reading.
        time.sleep(0.4)
        response = _read_http_response(client)
        assert "101 Switching Protocols" in response
        assert "\ufffd" not in response, "binary frame bytes leaked into the header read"
    finally:
        if client:
            client.close()
        server.stop()
        reset_manager()
