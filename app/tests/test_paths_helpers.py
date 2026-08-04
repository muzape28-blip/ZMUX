"""Tests for shared path helpers (display_path, example seeding)."""
from pathlib import Path

from zmux.paths import EXAMPLE_SCRIPTS, display_path, seed_examples


class TestDisplayPath:
    def test_home_renders_as_tilde(self, tmp_path):
        assert display_path(tmp_path, home=tmp_path) == "~"

    def test_subdir_renders_tilde_prefixed(self, tmp_path):
        sub = tmp_path / "projects" / "demo"
        sub.mkdir(parents=True)
        assert display_path(sub, home=tmp_path) == "~/projects/demo"

    def test_outside_home_renders_absolute(self, tmp_path):
        assert display_path(Path("/etc"), home=tmp_path) == "/etc"


class TestSeedExamples:
    def test_first_seed_writes_all_examples(self, tmp_path):
        target = seed_examples(home=tmp_path)
        assert target == tmp_path / "examples"
        for name, content in EXAMPLE_SCRIPTS.items():
            written = (target / name).read_text(encoding="utf-8")
            assert written == content
        assert (tmp_path / ".examples_seeded").exists()

    def test_second_seed_is_a_noop_and_keeps_user_edits(self, tmp_path):
        first = seed_examples(home=tmp_path)
        assert first is not None
        edited = first / "hello.py"
        edited.write_text("# user modified\n", encoding="utf-8")

        assert seed_examples(home=tmp_path) is None
        assert edited.read_text(encoding="utf-8") == "# user modified\n"

    def test_example_scripts_are_valid_python(self, tmp_path):
        for name, content in EXAMPLE_SCRIPTS.items():
            compile(content, name, "exec")  # raises SyntaxError if invalid

    def test_seeding_failure_returns_none(self, tmp_path):
        # A file named "examples" blocks directory creation -> graceful None.
        (tmp_path / "examples").write_text("blocked", encoding="utf-8")
        assert seed_examples(home=tmp_path) is None
