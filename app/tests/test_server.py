"""Tests for terminal-facing server response formatting."""

import socket

import pytest

from zmux.server import (
    CHROMIUM_RESTRICTED_PORTS,
    P4A_HTTP_PORT,
    _bind_listener,
    _bind_ws_socket,
    _format_zpip_output,
    _get_json_payload,
    app,
)


def test_get_json_payload_invalid_json_returns_2tuple_and_400():
    """Malformed JSON body must yield a 2-tuple so POST callers can unpack
    `payload, err = _get_json_payload()` without raising
    `ValueError: too many values to unpack` (regression for PR #14)."""
    with app.test_request_context(
        "/api/exec",
        method="POST",
        data=b"{not valid json",
        content_type="application/json",
    ):
        payload, err = _get_json_payload()
        assert payload is None
        # The error value must be a 2-tuple (Response, status), not a 3-tuple,
        # so Flask can return it directly via `return err`.
        assert isinstance(err, tuple) and len(err) == 2
        resp = app.make_response(err)
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "invalid_json"


def test_get_json_payload_non_object_returns_2tuple_and_400():
    """A JSON array/scalar is not a request object -> 400 with invalid_json_type."""
    with app.test_request_context(
        "/api/exec",
        method="POST",
        data=b"[1, 2, 3]",
        content_type="application/json",
    ):
        payload, err = _get_json_payload()
        assert payload is None
        assert isinstance(err, tuple) and len(err) == 2
        resp = app.make_response(err)
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "invalid_json_type"


def test_get_json_payload_valid_dict_returns_data():
    with app.test_request_context(
        "/api/exec",
        method="POST",
        data=b'{"command": "echo hi"}',
        content_type="application/json",
    ):
        payload, err = _get_json_payload()
        assert err is None
        assert payload == {"command": "echo hi"}


def test_get_json_payload_empty_body_returns_empty_dict():
    with app.test_request_context(
        "/api/exec",
        method="POST",
        data=b"",
        content_type="application/json",
    ):
        payload, err = _get_json_payload()
        assert err is None
        assert payload == {}


def test_install_output_uses_real_newlines():
    output, exit_code = _format_zpip_output(
        "zpip install demo",
        {
            "ok": True,
            "package": "demo",
            "version": "1.2.3",
            "dependencies_installed": ["dep-one", "dep-two"],
        },
    )

    assert exit_code == 0
    assert output == "Successfully installed demo-1.2.3\nAlso installed: dep-one, dep-two"


def test_failed_verify_keeps_useful_details():
    output, exit_code = _format_zpip_output(
        "zpip verify demo",
        {"ok": False, "package": "demo", "missing": ["demo/module.py"]},
    )

    assert exit_code == 1
    assert output == "demo verification failed: missing files: demo/module.py"


def test_doctor_reports_failed_packages_with_failure_exit_code():
    output, exit_code = _format_zpip_output(
        "zpip doctor",
        {
            "ok": False,
            "runtime": {
                "runtime_id": "runtime-test",
                "python": {"version": "3.14.2"},
                "android": {"abi": "arm64-v8a"},
                "paths": {"user_packages": "/tmp/packages"},
            },
            "free_bytes": 1024,
            "index": "https://example.invalid/index",
            "packages": {"broken": {"ok": False}},
        },
    )

    assert exit_code == 1
    assert "broken: FAILED" in output


def test_bind_listener_returns_live_listening_socket():
    sock = _bind_listener("127.0.0.1", 0)
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        assert port > 0
        # Must accept connections immediately (real listener, no probe race)
        probe = socket.create_connection(("127.0.0.1", port), timeout=2)
        probe.close()
    finally:
        sock.close()


def test_bind_ws_socket_skips_occupied_ports():
    http_sock = _bind_listener("127.0.0.1", 0)
    try:
        http_port = http_sock.getsockname()[1]
        blocker = _bind_listener("127.0.0.1", http_port + 1)
        try:
            ws_sock = _bind_ws_socket(http_port)
            try:
                assert ws_sock.getsockname()[1] > http_port + 1
            finally:
                ws_sock.close()
        finally:
            blocker.close()
    finally:
        http_sock.close()


class TestLoopbackOnlyBind:
    """"/" serves the WebView auth token unauthenticated, so no listener may
    ever bind a wildcard interface (0.0.0.0). These tests force every bind
    attempt to fail and record which hosts were tried."""

    def _recording_bind(self, attempts):
        def fake_bind(host, port, family=socket.AF_INET, reuse_port=False):
            attempts.append(host)
            raise OSError("blocked by test")
        return fake_bind

    def test_ws_socket_never_attempts_wildcard(self, monkeypatch):
        from zmux import server
        attempts = []
        monkeypatch.setattr(server, "_bind_listener", self._recording_bind(attempts))
        with pytest.raises(RuntimeError):
            server._bind_ws_socket(server.P4A_HTTP_PORT)
        assert attempts, "expected at least one bind attempt"
        assert set(attempts) == {"127.0.0.1"}
        assert "0.0.0.0" not in attempts

    def test_http_socket_never_attempts_wildcard_on_android(self, monkeypatch):
        from zmux import server
        attempts = []
        monkeypatch.setattr(server, "_bind_listener", self._recording_bind(attempts))
        monkeypatch.setenv("ANDROID_PRIVATE", "1")
        # Keep the retry loop short for the test.
        monkeypatch.setattr(server, "P4A_BIND_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(RuntimeError):
            server._bind_http_socket()
        assert attempts
        assert attempts[0] == "127.0.0.1"
        assert "0.0.0.0" not in attempts
        assert set(attempts) <= {"127.0.0.1", "localhost"}


class TestChromiumPortContract:
    """The p4a WebView bootstrap loads http://127.0.0.1:{p4a.port}/ through the
    Android WebView, which refuses Chromium's restricted ports with
    net::ERR_UNSAFE_PORT (ZMUX v1.0.x hit this on 6000/X11). The contract
    port must stay off that list and off Zabacode's port, and the WebSocket
    scanner must skip restricted ports too (the list applies to ws://)."""

    def test_p4a_http_port_not_chromium_restricted(self):
        assert P4A_HTTP_PORT not in CHROMIUM_RESTRICTED_PORTS

    def test_p4a_http_port_distinct_from_zabacode_and_its_ws_range(self):
        zabacode = 5000  # ZABACODE buildozer.spec p4a.port
        assert P4A_HTTP_PORT != zabacode
        # ZMUX's own ws range is P4A_HTTP_PORT+1..+100; it must never overlap
        # Zabacode's (5001..5100) or loopback cross-talk returns.
        assert not (zabacode <= P4A_HTTP_PORT <= zabacode + 100)

    def test_bind_http_socket_rejects_chromium_restricted_port(self, monkeypatch):
        from zmux import server
        monkeypatch.setattr(server, "P4A_HTTP_PORT", 6000)
        with pytest.raises(RuntimeError, match="ERR_UNSAFE_PORT"):
            server._bind_http_socket()

    def test_bind_ws_socket_skips_chromium_restricted_ports(self):
        # http_port 6565 -> first candidate is 6566 (sane-port), which is on
        # the restricted list; the scanner must jump to 6567.
        sock = _bind_ws_socket(6565)
        try:
            assert sock.getsockname()[1] == 6567
        finally:
            sock.close()


class TestWsPortConfig:
    """The announced WebSocket port lives in app.config, not a module global."""

    def test_default_ws_port_present(self):
        assert app.config["WS_PORT"] == P4A_HTTP_PORT + 1

    def test_csp_and_template_use_configured_port(self):
        old = app.config["WS_PORT"]
        app.config["WS_PORT"] = 59999
        try:
            client = app.test_client()
            resp = client.get("/")
            assert resp.status_code == 200
            assert f"ws://127.0.0.1:{59999}" in resp.headers["Content-Security-Policy"]
            # terminal.html receives the same port for its ws:// connect URL
            assert b"59999" in resp.data
        finally:
            app.config["WS_PORT"] = old


class TestLegacyExecSurface:
    """REST /api/exec is compatibility-only; the UI uses WebSocket PTY."""

    def _auth(self):
        from zmux.security import AUTH_TOKEN
        return {"X-ZMUX-Token": AUTH_TOKEN}

    def test_terminal_html_does_not_call_legacy_exec_endpoint(self):
        from zmux import server
        html = (server.BASE_DIR / "templates" / "terminal.html").read_text(encoding="utf-8")
        assert "/api/exec" not in html
        assert "fetch(" not in html

    def test_health_does_not_create_legacy_terminal_session(self, monkeypatch):
        from zmux import server
        monkeypatch.setattr(server, "get_session", lambda: (_ for _ in ()).throw(AssertionError("legacy session created")))
        resp = server.app.test_client().get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "idle"
        assert data["runtime"] == "alpine-pty"

    def test_legacy_exec_still_accepts_implicit_python_with_metadata(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "1 + 1"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["stdout"].strip() == "2"
        assert data["legacy"] is True
        assert data["legacy_input_mode"] == server.LEGACY_EXEC_INPUT_MODE
        assert data["explicit_language"] is False
        assert data["language"] == server.EXEC_LANGUAGE_DEFAULT

    def test_legacy_auto_language_is_explicit_alias_of_current_behavior(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "1 + 1", "language": "legacy-auto"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["stdout"].strip() == "2"
        assert data["explicit_language"] is True
        assert data["language"] == "legacy-auto"

    def test_python_language_executes_python_explicitly(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "print(21 + 21)", "language": "python"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["stdout"].strip() == "42"
        assert data["explicit_language"] is True
        assert data["language"] == "python"

    def test_python_language_bypasses_shell_builtins(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "ls", "language": "python"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert data["language"] == "python"
        assert "NameError" in data["stderr"]

    def test_command_language_runs_known_command(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "echo hello", "language": "command"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["stdout"] == "hello\n"
        assert data["explicit_language"] is True
        assert data["language"] == "command"

    def test_command_language_unknown_command_is_not_python(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "gti status", "language": "command"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert data["exit_code"] == 127
        assert "gti: command not found" in data["stderr"]
        assert data["language"] == "command"

    def test_command_language_does_not_evaluate_python_expression(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "1 + 1", "language": "command"},
            headers=self._auth(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert data["stdout"] != "2\n"
        assert data["exit_code"] == 127
        assert data["language"] == "command"

    def test_unknown_exec_language_is_rejected_without_execution(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "1 + 1", "language": "ruby"},
            headers=self._auth(),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data["code"] == "invalid_language"
        assert data["supported"] == ["command", "legacy-auto", "python"]
        assert data["planned"] == []

    def test_non_string_exec_language_is_rejected(self):
        from zmux import server
        resp = server.app.test_client().post(
            "/api/exec",
            json={"command": "1 + 1", "language": ["legacy-auto"]},
            headers=self._auth(),
        )
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "invalid_language"

    def test_status_prompt_input_and_stop_are_legacy_rest_endpoints(self):
        from zmux import server
        client = server.app.test_client()
        checks = [
            client.get("/api/status", headers=self._auth()),
            client.get("/api/prompt", headers=self._auth()),
            client.post("/api/input", json={"text": "hello"}, headers=self._auth()),
            client.post("/api/stop", headers=self._auth()),
        ]
        for resp in checks:
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["legacy"] is True
            assert data["legacy_endpoint"] is True
            assert data["legacy_status"] == "compatibility-only"
            assert "REST terminal session endpoints" in data["deprecation"]
