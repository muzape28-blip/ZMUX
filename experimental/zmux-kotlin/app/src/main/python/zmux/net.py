"""
ZMUX Core — Shared TLS/SSL Context

Android (python-for-android) ships no readable system CA trust store, so a bare
``urllib.request.urlopen`` fails every HTTPS call with:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

This module resolves a *verified* SSL context once, preferring the bundled
``certifi`` CA bundle, and exposes it to every outbound HTTPS caller.

Security note: we never silently fall back to an unverified context. Callers
that want to degrade must do so explicitly and tell the user.
"""

import ssl
from functools import lru_cache

__all__ = ["get_ssl_context", "ca_bundle_available", "TLS_HELP_MESSAGE"]

TLS_HELP_MESSAGE = (
    "TLS certificate verification failed. The device has no usable CA bundle. "
    "Rebuild the APK with 'certifi' listed in buildozer.spec requirements."
)


@lru_cache(maxsize=1)
def _resolve_context() -> tuple:
    """Build an SSL context, preferring certifi's CA bundle. Cached per process."""
    # 1. Preferred: certifi bundle shipped inside the APK.
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx, True
    except Exception:
        pass

    # 2. Fallback: whatever the platform exposes (works on desktop/CI).
    try:
        ctx = ssl.create_default_context()
        # get_ca_certs() empty => no trust anchors loaded => verification will fail.
        if ctx.get_ca_certs():
            return ctx, True
        return ctx, False
    except Exception:
        return ssl.create_default_context(), False


def get_ssl_context() -> ssl.SSLContext:
    """Return a certificate-verifying SSL context for outbound HTTPS."""
    return _resolve_context()[0]


def ca_bundle_available() -> bool:
    """True when real trust anchors were loaded (i.e. verification can succeed)."""
    return _resolve_context()[1]
