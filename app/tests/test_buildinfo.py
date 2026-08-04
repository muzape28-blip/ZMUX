"""Tests for zmux.buildinfo — the on-device build marker."""
import pytest

from zmux import buildinfo


def test_marker_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(buildinfo, "marker_path", lambda: tmp_path / "nope.txt")
    assert buildinfo.build_marker() == ""
    assert buildinfo.short_marker() == ""


def test_marker_reads_content(monkeypatch, tmp_path):
    marker = tmp_path / "build_marker.txt"
    marker.write_text("0123456789abcdef run=12345\n", encoding="utf-8")
    monkeypatch.setattr(buildinfo, "marker_path", lambda: marker)
    assert buildinfo.build_marker() == "0123456789abcdef run=12345"
    assert buildinfo.short_marker() == "0123456789abcdef"


def test_marker_path_sits_next_to_package():
    # app/zmux/buildinfo.py -> app/build_marker.txt (same dir private.tar is
    # built from), so the marker actually ends up in the APK.
    from pathlib import Path
    assert buildinfo.marker_path() == Path(buildinfo.__file__).resolve().parent.parent / "build_marker.txt"
