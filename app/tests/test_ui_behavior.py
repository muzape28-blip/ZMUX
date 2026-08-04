"""Behavioral tests for the terminal UI (app/templates/terminal.html).

Two layers, mirroring the repo's test style:

1. **Real execution** — the production inline script is extracted verbatim
   from terminal.html and run under Node (V8, the same engine family as the
   Android WebView) by ``app/tests/ui_harness.js``. The harness replays
   synthetic touch/wheel/key sequences against the shipped event handlers
   on a deterministic clock: tab hold-to-close, keyword-bar toggle, and
   terminal scroll-follow are all asserted end to end. Skipped only where
   no Node runtime exists (GitHub's CI images ship one).
2. **Structure** — the page rendered by the real Flask app is checked for
   the new affordances (and for the absence of the removed ones).
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

APP_TESTS = Path(__file__).parent
HARNESS = APP_TESTS / "ui_harness.js"
TEMPLATE = APP_TESTS.parent / "templates" / "terminal.html"

NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="node runtime not available")


def run_ui_harness(template: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(HARNESS), str(template)],
        capture_output=True,
        text=True,
        timeout=120,
    )


@requires_node
def test_ui_behaviors_pass_on_shipped_template():
    """The harness executes the real inline script of the shipped template.

    Covers: tap-to-switch, 1.5s hold-to-close (visual feedback, vibrate,
    move/finger-release cancellation, click suppression), keyword-bar
    toggle (default visible, localStorage persistence, refit), and
    scroll-follow (no yank while reading, \\x1b[2J reset, resize safety).
    """
    result = run_ui_harness(TEMPLATE)
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"UI harness failed:\n{output}"
    assert "not ok" not in output
    summary = re.search(r"# (\d+)/(\d+) passed", output)
    assert summary, f"harness summary missing:\n{output}"
    assert int(summary.group(1)) == int(summary.group(2)) >= 40


@requires_node
def test_ui_harness_detects_the_regressions_it_guards(tmp_path):
    """Guard against vacuous tests: an OLD-style template (close button,
    latching onScroll, no toggle) must FAIL the harness."""
    old = TEMPLATE.read_text(encoding="utf-8")
    # Reintroduce the old, buggy onScroll latch and drop the clear-reset.
    mutated = old.replace(
        "if (scrollTop >= (term.buffer.active.baseY || 0)) userScrolledUp = false;",
        "if (scrollTop < lastScrollTop) userScrolledUp = true;").replace(
        "writeTerminalOutput(bytes, bytesContainClearScreen(bytes));",
        "term.write(bytes); if (!userScrolledUp) term.scrollToBottom();")
    assert mutated != old, "template markers changed — update this mutation"
    mutant = tmp_path / "terminal_mutant.html"
    mutant.write_text(mutated, encoding="utf-8")
    result = run_ui_harness(mutant)
    assert result.returncode != 0
    assert "not ok" in result.stdout


class TestTemplateStructure:
    """The rendered page exposes (and hides) the right affordances."""

    def _render(self) -> str:
        from zmux.server import app
        with app.test_client() as client:
            resp = client.get("/")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_keys_toggle_button_present_in_topbar(self):
        html = self._render()
        assert 'id="keys-toggle"' in html
        # It sits inside the topbar, next to the "> ZMUX" title.
        topbar = re.search(r'<div id="topbar">.*?</div>', html, re.S).group(0)
        assert 'id="keys-toggle"' in topbar and "ZMUX" in topbar

    def test_close_button_removed_from_tabs(self):
        html = self._render()
        assert 'class=\'close\'' not in html and 'class="close"' not in html
        assert "session.close" in html  # close still exists — via hold now

    def test_hold_to_close_constants_present(self):
        html = self._render()
        assert "HOLD_TO_CLOSE_MS = 1500" in html
        assert "navigator.vibrate" in html
        assert "touchend" in html and "touchmove" in html

    def test_keys_bar_persistence_wired(self):
        html = self._render()
        assert "localStorage" in html
        assert "zmux.keysBar.visible" in html
        assert "fitTerminal();" in html  # refit after toggle

    def test_scroll_handlers_wired(self):
        html = self._render()
        assert "bindScrollIntent" in html
        assert "writeTerminalOutput" in html
        assert "bytesContainClearScreen" in html
        # The x1b[2J reset is the regression fix for the frozen viewport.
        assert "\\x1b[2J" in html

    def test_harness_file_is_the_only_js_runner(self):
        assert HARNESS.is_file()
        # The harness must keep testing the TEMPLATE's own script, not a copy.
        assert HARNESS.read_text(encoding="utf-8").count("extractMainScript") >= 2
