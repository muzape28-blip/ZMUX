"""Tests for zpip search: real curated index + installed db + exact PyPI probe.

Every network path is stubbed — these tests must pass with the cable pulled.
The previous implementation searched a hardcoded list of 13 names; there is a
dedicated regression test proving that list is gone.
"""
import json
import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))


@pytest.fixture
def zpip(monkeypatch, tmp_path):
    """zmux.zpip with an isolated catalog cache, empty installed db, and — by
    default — a dead network. Tests that exercise live fetch/probe paths stub
    ``_request_json`` explicitly; everything else must stay hermetic even on
    a machine that happens to be online."""
    from zmux import zpip as module

    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(module, "_load_db", lambda: {})
    monkeypatch.delenv("ZMUX_OFFLINE", raising=False)

    def dead_network(url, timeout=25):
        raise OSError(f"fixture default: no network ({url})")

    monkeypatch.setattr(module, "_request_json", dead_network)
    return module


def _http_stub(*, catalog=None, catalog_error=None, pypi=None, pypi_error=None):
    """Route fake JSON responses by URL, like the real endpoints would."""
    def fake_request_json(url, timeout=25):
        if "pypi.org" in url:
            if pypi_error is not None:
                raise pypi_error
            if pypi is None:
                raise OSError("connection refused")
            return {"info": pypi}
        if catalog_error is not None:
            raise catalog_error
        return {"schema_version": 1, "packages": catalog or {}}

    return fake_request_json


def _cache_file(zpip):
    fp = zpip.runtime_fingerprint()
    return zpip._catalog_cache_path(fp["runtime_id"], fp["android"]["abi"])


def _seed_cache(zpip, packages, *, age_seconds=0):
    path = _cache_file(zpip)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "fetched_at": time.time() - age_seconds,
            "packages": packages,
        }),
        encoding="utf-8",
    )
    return path


SAMPLE_CATALOG = {
    "requests": {
        "name": "requests",
        "version": "2.32.3",
        "summary": "Python HTTP for Humans.",
        "channel": "stable",
    },
    "http-toolbox": {
        "name": "http-toolbox",
        "version": "0.4.0",
        "summary": "Small HTTP client helpers and retry utilities.",
        "channel": "candidate",
    },
    "tinydb": {
        "name": "tinydb",
        "version": "4.8.0",
        "summary": "Tiny, document oriented database.",
        "channel": "stable",
    },
}


class TestCuratedCatalog:
    def test_live_hit_writes_cache(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog=SAMPLE_CATALOG))
        result = zpip.search("requests")
        assert result["ok"]
        assert result["sources"]["curated"] == "live"
        assert any(e["name"] == "requests" and e["source"] == "curated"
                   for e in result["results"])
        # The live fetch must leave an on-disk cache for offline searches.
        cached = json.loads(_cache_file(zpip).read_text(encoding="utf-8"))
        assert cached["packages"]["requests"]["version"] == "2.32.3"

    def test_fresh_cache_used_when_network_down(self, zpip, monkeypatch):
        _seed_cache(zpip, SAMPLE_CATALOG)
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog_error=OSError("down")))
        result = zpip.search("tinydb")
        assert result["ok"]
        assert result["sources"]["curated"] == "cache"
        assert [e["name"] for e in result["results"]] == ["tinydb"]

    def test_stale_cache_is_labeled_stale(self, zpip, monkeypatch):
        _seed_cache(zpip, SAMPLE_CATALOG, age_seconds=zpip.CATALOG_TTL_SECONDS + 60)
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog_error=OSError("down")))
        result = zpip.search("tinydb")
        assert result["sources"]["curated"] == "stale"
        assert result["results"][0]["name"] == "tinydb"

    def test_no_cache_no_network_is_unavailable(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog_error=OSError("down")))
        result = zpip.search("tinydb")
        assert result["ok"]
        assert result["sources"]["curated"] == "unavailable"
        assert result["results"] == []

    def test_malformed_live_payload_falls_back_to_cache(self, zpip, monkeypatch):
        _seed_cache(zpip, SAMPLE_CATALOG)
        monkeypatch.setattr(zpip, "_request_json", lambda url, timeout=25: {"nope": True})
        result = zpip.search("requests")
        assert result["sources"]["curated"] == "cache"
        assert result["results"][0]["name"] == "requests"


class TestOfflineMode:
    def test_offline_reads_cache_without_network(self, zpip, monkeypatch):
        _seed_cache(zpip, SAMPLE_CATALOG)
        monkeypatch.setenv("ZMUX_OFFLINE", "1")

        def explode(url, timeout=25):
            raise AssertionError(f"network must not be touched offline: {url}")

        monkeypatch.setattr(zpip, "_request_json", explode)
        result = zpip.search("requests")
        assert result["ok"]
        assert result["sources"] == {"curated": "cache", "pypi": "skipped"}
        assert result["results"][0]["name"] == "requests"

    def test_offline_without_cache_reports_unavailable(self, zpip, monkeypatch):
        monkeypatch.setenv("ZMUX_OFFLINE", "1")
        result = zpip.search("requests")
        assert result["sources"]["curated"] == "unavailable"
        assert result["sources"]["pypi"] == "skipped"
        assert result["results"] == []


class TestMatching:
    def test_multiword_query_requires_all_tokens(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_fetch_curated_catalog",
                            lambda: (SAMPLE_CATALOG, "live"))
        result = zpip.search("http client")
        names = [e["name"] for e in result["results"]]
        # "http-toolbox" matches both tokens in name+summary; "requests"
        # matches "http" but has no "client" anywhere.
        assert names == ["http-toolbox"]

    def test_exact_name_ranks_first(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_fetch_curated_catalog",
                            lambda: (SAMPLE_CATALOG, "live"))
        result = zpip.search("requests")
        assert result["results"][0]["name"] == "requests"

    def test_summary_only_match_ranks_below_name_match(self, zpip, monkeypatch):
        catalog = {
            "alpha": {"name": "alpha", "version": "1", "summary": "has zeta inside"},
            "zeta-lib": {"name": "zeta-lib", "version": "1", "summary": ""},
        }
        monkeypatch.setattr(zpip, "_fetch_curated_catalog", lambda: (catalog, "live"))
        result = zpip.search("zeta")
        assert [e["name"] for e in result["results"]] == ["zeta-lib", "alpha"]

    def test_separator_insensitive_name_matching(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_fetch_curated_catalog",
                            lambda: (SAMPLE_CATALOG, "live"))
        result = zpip.search("http_toolbox")
        assert result["results"][0]["name"] == "http-toolbox"


class TestPypiProbe:
    def test_exact_probe_for_uncurated_name(self, zpip, monkeypatch):
        stub = _http_stub(catalog={}, pypi={
            "name": "requests", "version": "2.32.3",
            "summary": "Python HTTP for Humans.",
        })
        monkeypatch.setattr(zpip, "_request_json", stub)
        result = zpip.search("requests")
        assert result["sources"]["pypi"] == "live"
        entry = result["results"][0]
        assert entry["source"] == "pypi"
        assert entry["version"] == "2.32.3"
        assert entry["installed"] is False

    def test_probe_skipped_when_curated_has_exact_match(self, zpip, monkeypatch):
        calls = []
        real_stub = _http_stub(catalog=SAMPLE_CATALOG)
        monkeypatch.setattr(zpip, "_request_json",
                            lambda url, timeout=25: (calls.append(url), real_stub(url))[1])
        result = zpip.search("requests")
        assert result["sources"]["pypi"] == "skipped"
        assert not any("pypi.org" in url for url in calls)
        assert result["results"][0]["source"] == "curated"

    def test_probe_skipped_for_multiword_queries(self, zpip, monkeypatch):
        calls = []
        monkeypatch.setattr(zpip, "_request_json",
                            lambda url, timeout=25: calls.append(url) or {"packages": {}})
        result = zpip.search("http client")
        assert result["sources"]["pypi"] == "skipped"
        assert not any("pypi.org" in url for url in calls)

    def test_unreachable_pypi_is_reported(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_request_json",
                            _http_stub(catalog={}, pypi_error=OSError("no route")))
        result = zpip.search("requests")
        assert result["sources"]["pypi"] == "unavailable"
        assert result["results"] == []

    def test_pypi_entry_marks_installed_package(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_load_db", lambda: {"requests": {"version": "2.31.0"}})
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog={}, pypi={
            "name": "requests", "version": "2.32.3", "summary": "Python HTTP for Humans.",
        }))
        result = zpip.search("requests")
        entry = result["results"][0]
        assert entry["source"] == "pypi"
        assert entry["installed"] is True


class TestInstalledDatabase:
    def test_installed_only_match_honest_source(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_load_db", lambda: {"tinydb": {"version": "4.8.0"}})
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog_error=OSError("down")))
        result = zpip.search("tinydb")
        assert result["sources"]["curated"] == "unavailable"
        entry = result["results"][0]
        assert entry["source"] == "installed"
        assert entry["version"] == "4.8.0"
        assert entry["installed"] is True

    def test_curated_entry_for_installed_package_is_flagged(self, zpip, monkeypatch):
        monkeypatch.setattr(zpip, "_load_db", lambda: {"requests": {"version": "2.32.3"}})
        monkeypatch.setattr(zpip, "_request_json", _http_stub(catalog=SAMPLE_CATALOG))
        result = zpip.search("requests")
        entry = result["results"][0]
        assert entry["source"] == "curated"
        assert entry["installed"] is True


class TestHonestyFloor:
    def test_no_hardcoded_package_list_anymore(self, zpip, monkeypatch):
        """Regression: old search() returned a baked-in list of 13 names even
        with zero data sources. With everything empty it must say 'nothing'."""
        monkeypatch.setattr(zpip, "_request_json",
                            _http_stub(catalog={}, pypi_error=OSError("down")))
        result = zpip.search("requests")
        assert result["ok"]
        assert result["results"] == []

    def test_empty_query_is_an_error(self, zpip):
        result = zpip.search("   ")
        assert not result["ok"]
        assert "empty" in result["error"]

    def test_garbage_query_is_an_error(self, zpip):
        result = zpip.search("?!*")
        assert not result["ok"]


class TestDispatch:
    def test_dispatch_multiword_search(self, zpip):
        result = zpip.dispatch("zpip search http client")
        assert result["ok"]
        assert isinstance(result["results"], list)

    def test_dispatch_search_without_query_is_usage_error(self, zpip):
        result = zpip.dispatch("zpip search")
        assert not result["ok"]
        assert "invalid arguments" in result["error"]

    def test_dispatch_search_echoes_query(self, zpip):
        result = zpip.dispatch("zpip search tinydb")
        assert result["query"] == "tinydb"


class TestFormatOutput:
    def _render(self, zpip, result):
        return zpip.format_output("zpip search x", result)

    def test_entry_rendering(self, zpip):
        text, code = self._render(zpip, {
            "ok": True,
            "results": [{
                "name": "requests", "version": "2.32.3",
                "summary": "Python HTTP for Humans.",
                "source": "curated", "installed": True,
            }],
            "sources": {"curated": "live", "pypi": "skipped"},
        })
        assert code == 0
        assert text == "requests 2.32.3 [curated,installed] - Python HTTP for Humans."

    def test_stale_cache_note(self, zpip):
        text, _ = self._render(zpip, {
            "ok": True,
            "results": [{"name": "tinydb", "version": "4.8.0", "summary": "",
                         "source": "curated", "installed": False}],
            "sources": {"curated": "stale", "pypi": "skipped"},
        })
        assert "tinydb 4.8.0 [curated]" in text
        assert "stale cache" in text

    def test_unavailable_sources_are_disclosed(self, zpip):
        text, _ = self._render(zpip, {
            "ok": True,
            "results": [{"name": "tinydb", "version": "4.8.0", "summary": "",
                         "source": "installed", "installed": True}],
            "sources": {"curated": "unavailable", "pypi": "unavailable"},
        })
        assert "curated index unavailable" in text
        assert "pypi.org unreachable" in text

    def test_no_results(self, zpip):
        text, code = self._render(zpip, {"ok": True, "results": [], "sources": {}})
        assert (text, code) == ("No packages found", 0)

    def test_no_results_still_discloses_unreachable_sources(self, zpip):
        text, code = self._render(zpip, {
            "ok": True, "results": [],
            "sources": {"curated": "unavailable", "pypi": "unavailable"},
        })
        assert code == 0
        assert "No packages found" in text
        assert "curated index unavailable" in text
        assert "pypi.org unreachable" in text

    def test_error_result(self, zpip):
        text, code = self._render(zpip, {"ok": False, "error": "empty search query"})
        assert code == 1
        assert "empty search query" in text
