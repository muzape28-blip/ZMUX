"""
ZMUX Core — Local Encrypted Storage (at-rest protection)

Uses:
  * a 256-bit secret generated randomly **per installation**, stored in a
    separate 0600 file and never committed;
  * a HMAC-SHA256 keystream in counter mode (encrypt-then-MAC);
  * a random 16-byte nonce per write, so identical contents differ each time;
  * an HMAC-SHA256 authentication tag verified in constant time, so tampered
    or truncated files are rejected instead of silently decrypting to garbage.

Stdlib only: ``cryptography`` requires a Rust toolchain that python-for-android
frequently fails to cross-compile for ARM, which would break the APK build.
"""

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from zmux.paths import APP_DIR

__all__ = ["encrypt_payload", "decrypt_payload", "MASTER_KEY_FILE"]

MASTER_KEY_FILE = APP_DIR / ".zmux_master_key"
_PBKDF2_ROUNDS = 200_000
_NONCE_BYTES = 16
_TAG_BYTES = 32
_FORMAT_VERSION = 1


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod 0600 (owner read/write only)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _load_or_create_master_secret() -> bytes:
    """Return this installation's 256-bit master secret, creating it on first use."""
    if MASTER_KEY_FILE.exists():
        try:
            raw = MASTER_KEY_FILE.read_text(encoding="utf-8").strip()
            secret = bytes.fromhex(raw)
            if len(secret) == 32:
                _restrict_permissions(MASTER_KEY_FILE)
                return secret
        except Exception:
            pass  # corrupted -> regenerate below

    secret = secrets.token_bytes(32)
    try:
        MASTER_KEY_FILE.write_text(secret.hex(), encoding="utf-8")
        _restrict_permissions(MASTER_KEY_FILE)
    except Exception:
        pass  # ephemeral secret; keys simply won't persist this run
    return secret


def _derive_keys(nonce: bytes) -> tuple:
    """Derive independent (encryption, authentication) keys for one payload."""
    material = hashlib.pbkdf2_hmac(
        "sha256", _load_or_create_master_secret(), nonce, _PBKDF2_ROUNDS, dklen=64
    )
    return material[:32], material[32:]


def _keystream(key: bytes, length: int) -> bytes:
    """HMAC-SHA256 in counter mode — a standard PRF-based stream cipher."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_payload(data: dict) -> str:
    """Encrypt a dict to an authenticated, versioned JSON envelope."""
    plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    enc_key, mac_key = _derive_keys(nonce)

    ciphertext = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc_key, len(plaintext))))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()

    return json.dumps({
        "v": _FORMAT_VERSION,
        "nonce": nonce.hex(),
        "data": ciphertext.hex(),
        "tag": tag.hex(),
    })


def decrypt_payload(envelope: str) -> dict:
    """Decrypt and authenticate an envelope. Returns {} on any failure."""
    try:
        parsed = json.loads(envelope)
        if parsed.get("v") != _FORMAT_VERSION:
            return {}

        nonce = bytes.fromhex(parsed["nonce"])
        ciphertext = bytes.fromhex(parsed["data"])
        tag = bytes.fromhex(parsed["tag"])
        if len(nonce) != _NONCE_BYTES or len(tag) != _TAG_BYTES:
            return {}

        enc_key, mac_key = _derive_keys(nonce)

        # Verify before decrypting; constant-time to avoid a forgery oracle.
        expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            return {}

        plaintext = bytes(a ^ b for a, b in zip(ciphertext, _keystream(enc_key, len(ciphertext))))
        result = json.loads(plaintext.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}
