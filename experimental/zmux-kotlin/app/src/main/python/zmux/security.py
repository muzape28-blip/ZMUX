"""
ZMUX Core — Security: Auth Token Management
Handles local session auth tokens for WebView security.
"""

import hmac
import os
import secrets
import stat
from pathlib import Path

from zmux.paths import APP_DIR

# ---------------------------------------------------------------------------
# Auth Token Management
# ---------------------------------------------------------------------------

TOKEN_FILE = APP_DIR / ".zmux_auth_token"


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod 0600 (owner read/write only)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _load_or_create_token() -> str:
    """Load existing auth token or generate a new 128-bit hex token."""
    if TOKEN_FILE.exists():
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(token) >= 16:
                _restrict_permissions(TOKEN_FILE)
                return token
        except Exception:
            pass

    token = secrets.token_hex(16)
    try:
        TOKEN_FILE.write_text(token, encoding="utf-8")
        _restrict_permissions(TOKEN_FILE)
    except Exception:
        pass
    return token


AUTH_TOKEN = _load_or_create_token()


def verify_token(token: str) -> bool:
    """Verify an auth token using constant-time comparison."""
    if not token:
        return False
    return hmac.compare_digest(token, AUTH_TOKEN)
