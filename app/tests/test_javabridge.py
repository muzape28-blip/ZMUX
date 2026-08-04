"""Tests for zmux.javabridge — the Android Java bridge.

On desktop (CI) the bridge must degrade gracefully: prime() reports False,
accessors return None, and nothing raises. The on-device path (class resolved
on the main thread, cached for worker threads) cannot run here — it is guarded
by the same env checks the rest of the app uses.
"""
from zmux import javabridge


def test_prime_is_false_off_android():
    assert javabridge.prime() is False


def test_prime_is_idempotent():
    assert javabridge.prime() is javabridge.prime()


def test_accessors_are_none_off_android():
    assert javabridge.activity_class() is None
    assert javabridge.mActivity() is None
    assert javabridge.error() is not None


def test_activity_class_name_is_the_p4a_activity():
    # ZMUX uses the p4a webview bootstrap, whose activity class is this exact
    # name; it is what android.permissions/android.runnable also reference.
    assert javabridge.ACTIVITY_CLASS_NAME == "org.kivy.android.PythonActivity"
