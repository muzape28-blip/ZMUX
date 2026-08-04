"""Tests for ZMUX security module."""
import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))


class TestAuthToken:
    """Test auth token management."""

    def test_token_generated(self):
        from zmux.security import AUTH_TOKEN
        assert AUTH_TOKEN
        assert len(AUTH_TOKEN) >= 16

    def test_verify_valid_token(self):
        from zmux.security import AUTH_TOKEN, verify_token
        assert verify_token(AUTH_TOKEN) is True

    def test_verify_invalid_token(self):
        from zmux.security import verify_token
        assert verify_token("invalid_token") is False

    def test_verify_empty_token(self):
        from zmux.security import verify_token
        assert verify_token("") is False


class TestKeystore:
    """Test encrypted keystore."""

    def test_encrypt_decrypt_roundtrip(self):
        from zmux.keystore import encrypt_payload, decrypt_payload
        data = {"key": "value", "number": 42}
        encrypted = encrypt_payload(data)
        decrypted = decrypt_payload(encrypted)
        assert decrypted == data

    def test_tampered_data_rejected(self):
        from zmux.keystore import encrypt_payload, decrypt_payload
        data = {"key": "value"}
        encrypted = encrypt_payload(data)
        
        # Tamper with the data
        import json
        parsed = json.loads(encrypted)
        parsed["data"] = "0" * len(parsed["data"])
        tampered = json.dumps(parsed)
        
        # Should return empty dict on tampered data
        result = decrypt_payload(tampered)
        assert result == {}

    def test_invalid_format_returns_empty(self):
        from zmux.keystore import decrypt_payload
        assert decrypt_payload("not json") == {}
        assert decrypt_payload("{}") == {}
        assert decrypt_payload('{"v": 999}') == {}


class TestServerAuth:
    """Test server authentication."""

    def test_health_no_auth_required(self):
        """Health endpoint should not require auth."""
        from zmux.server import app
        with app.test_client() as client:
            resp = client.get("/api/health")
            assert resp.status_code == 200

    def test_exec_requires_auth(self):
        """Exec endpoint should require auth."""
        from zmux.server import app
        with app.test_client() as client:
            resp = client.post("/api/exec", json={"command": "echo test"})
            assert resp.status_code == 401

    def test_exec_with_valid_auth(self):
        """Exec endpoint should work with valid auth."""
        from zmux.server import app
        from zmux.security import AUTH_TOKEN
        with app.test_client() as client:
            resp = client.post(
                "/api/exec",
                json={"command": "echo test"},
                headers={"X-ZMUX-Token": AUTH_TOKEN},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"]
