"""Tests for legacy ZABAWHEELS/zpip package path boundaries."""

import os
import sys


def test_core_and_legacy_runtime_dirs_are_explicit_and_disjoint():
    from zmux import paths

    assert paths.HOME_DIR in paths.CORE_RUNTIME_DIRS
    assert paths.BIN_DIR in paths.CORE_RUNTIME_DIRS
    assert paths.USER_PACKAGES_DIR in paths.LEGACY_PACKAGE_DIRS
    assert paths.INSTALLED_DIR in paths.LEGACY_PACKAGE_DIRS
    assert paths.STAGING_DIR in paths.LEGACY_PACKAGE_DIRS
    assert set(paths.CORE_RUNTIME_DIRS).isdisjoint(paths.LEGACY_PACKAGE_DIRS)
    assert set(paths.ALL_RUNTIME_DIRS) == set(paths.CORE_RUNTIME_DIRS) | set(paths.LEGACY_PACKAGE_DIRS)


def test_legacy_package_paths_are_named_for_migration():
    from zmux import paths

    mapping = paths.legacy_package_paths()
    assert mapping == {
        "downloads": paths.DOWNLOADS_DIR,
        "staging": paths.STAGING_DIR,
        "user_packages": paths.USER_PACKAGES_DIR,
        "installed": paths.INSTALLED_DIR,
    }


def test_legacy_user_packages_pythonpath_prepends_without_dropping_existing():
    from zmux import paths

    existing = os.pathsep.join(["/already", "/there"])
    value = paths.legacy_user_packages_pythonpath(existing)
    assert value.split(os.pathsep) == [str(paths.USER_PACKAGES_DIR), "/already", "/there"]
    assert paths.legacy_user_packages_pythonpath("") == str(paths.USER_PACKAGES_DIR)


def test_legacy_user_packages_still_importable_for_compatibility():
    from zmux import paths

    assert str(paths.USER_PACKAGES_DIR) in sys.path


def test_runtime_info_reads_legacy_paths_without_importing_zpip():
    from zmux import runtime_info
    from zmux.paths import legacy_package_paths

    assert runtime_info.LEGACY_PACKAGE_PATHS == legacy_package_paths()
    assert runtime_info.DB_FILE == legacy_package_paths()["installed"] / "packages.json"


def test_runtime_info_labels_legacy_package_db_as_compatibility_only():
    from zmux import runtime_info

    fp = runtime_info.runtime_fingerprint()
    assert fp["legacy_package_db"]["status"] == "compatibility-only"
    assert fp["legacy_package_db"]["manager"] == "zpip"
    rendered = runtime_info.format_fingerprint(fp)
    assert "Legacy user packages:" in rendered
    assert "Installed packages (legacy zpip):" in rendered
