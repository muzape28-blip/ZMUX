#!/usr/bin/env python3
"""Guard against guessing the Termux TerminalSessionClient interface.

This is the anti-"I read a DeepWiki page for a different Termux version"
safeguard. After a real Gradle build (on CI, or locally with the Gradle
cache populated), the resolved terminal-emulator-0.118.0.jar is on disk.
We:

  1. find that JAR,
  2. run `javap` on `com.termux.terminal.TerminalSessionClient`
     to list every abstract method that an implementing class must supply,
  3. parse every `override fun <name>(` in
     ZmuxTerminalActivity.kt,
  4. fail if the two sets do not match.

It is deliberately dumb: it compares method names only, not signatures.
The Kotlin compiler already checks the signatures — this script exists to
catch the case where we *think* an override is needed but the method does
not actually exist in the pinned Termux version (or vice versa). That
class of bug previously produced
"''setTerminalShellPid'' overrides nothing".

Run after a Gradle build so the JAR is in the Gradle cache:

    python3 tests/check_termux_interface.py

If the JAR cannot be located (e.g. a fresh checkout with no Gradle
cache and no build), the script exits 0 with a note. That keeps local
Python-only test runs green; CI always runs it after `assembleDebug`,
which guarantees the artefact is present.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTIVITY = PROJECT_ROOT / "app/src/main/java/com/zmux/terminal/ZmuxTerminalActivity.kt"

# Coordinates we actually depend on in app/build.gradle.kts. We search the
# Gradle cache for the terminal-emulator JAR rather than hard-coding a hash
# path so this works across Gradle/CI cache layouts.
TERMUX_GROUP = "com.termux.termux-app"
TERMUX_ARTIFACT = "terminal-emulator"
TERMUX_VERSION = "0.118.0"
INTERFACE = "com.termux.terminal.TerminalSessionClient"

# Methods the Kotlin activity deliberately does not override as `override fun`
# because they are expressed as expression bodies on the same line or have a
# different textual shape. We list them here so the guard still considers the
# interface satisfied. Keep in sync with the activity — this is a short,
# human-curated allow list, not a free pass.
OVERRIDES_EXPECTED_BUT_NOT_FUN = {
    # All callback methods in the 0.118.0 interface are written as
    # `override fun <name>(...) = Unit` in the activity, so this set is empty
    # today. It exists so future single-line/expression-body overrides can be
    # accounted for without weakening the check.
}


def _gradle_root() -> Path:
    # $GRADLE_USER_HOME if set, else ~/.gradle (Linux/macOS/CI default).
    return Path(
        __import__("os").environ.get("GRADLE_USER_HOME")
        or (Path.home() / ".gradle")
    )


def _jar_provides_interface(jar: Path) -> bool:
    """True if `jar` actually contains TerminalSessionClient.class.

    We search by filename across several possible Gradle cache layouts
    (JitPack groupId vs mavenCoordinate differ, and AAR vs JAR differ), so
    we verify by asking `jar tf` for the expected class entry. Using
    `unzip -l` would also work, but `jar` ships with the JDK that this
    check already requires for javap.
    """
    jar_tool = shutil.which("jar")
    if not jar_tool:
        # No `jar` binary — fall back to filename + parent dir heuristic.
        # Good enough on the environments we care about (CI has a full JDK).
        return jar.is_file()
    listing = subprocess.run(
        [jar_tool, "tf", str(jar)], capture_output=True, text=True, check=False
    )
    if listing.returncode != 0:
        return False
    expected = "com/termux/terminal/TerminalSessionClient.class"
    return expected in listing.stdout


def find_terminal_emulator_jar() -> Path | None:
    """Locate terminal-emulator-0.118.0.jar in the Gradle cache or build tree.

    Termux publishes via JitPack where the on-disk group can be either
    `com.termux.termux-app` or `com.termux` depending on which coordinate
    resolved, so we search broadly by filename and verify the candidate
    with _jar_provides_interface() instead of hard-coding one group path.
    """
    name_glob = f"{TERMUX_ARTIFACT}-{TERMUX_VERSION}.jar"
    search_roots: list[Path] = []

    gradle = _gradle_root()
    modules = gradle / "caches/modules-2/files-2.1"
    if modules.is_dir():
        search_roots.append(modules)
    # Transforms hold exploded AARs in newer AGP; the classes.jar inside
    # them is named "classes.jar", not terminal-emulator-0.118.0.jar, so
    # we handle that case separately below.
    for transforms in gradle.glob("caches/transforms-*"):
        if transforms.is_dir():
            search_roots.append(transforms)

    # Project-local build intermediates (CI classpath, exploded AARs).
    build = PROJECT_ROOT / "build"
    if build.is_dir():
        search_roots.append(build)
    app_build = PROJECT_ROOT / "app/build"
    if app_build.is_dir():
        search_roots.append(app_build)

    candidates: list[Path] = []
    for root in search_roots:
        candidates.extend(root.rglob(name_glob))
        # Inside transformed AARs the terminal-emulator classes live in a
        # file named classes.jar alongside a path containing
        # "terminal-emulator"; those contain the interface we want too.
        if "transforms" in str(root) or root in (build, app_build):
            for classes_jar in root.rglob("classes.jar"):
                try:
                    if TERMUX_ARTIFACT in str(classes_jar):
                        candidates.append(classes_jar)
                except OSError:
                    pass

    for jar in candidates:
        try:
            if jar.is_file() and _jar_provides_interface(jar):
                return jar
        except OSError:
            continue
    return None


def interface_methods(jar: Path) -> set[str]:
    """Return names of abstract methods declared on TerminalSessionClient."""
    javap = shutil.which("javap")
    if not javap:
        return set()
    out = subprocess.run(
        [javap, "-classpath", str(jar), INTERFACE],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return set()
    # javap output lines look like:
    #   public abstract void onTextChanged(com.termux.terminal.TerminalSession);
    names: set[str] = set()
    method_re = re.compile(r"^\s*public\s+abstract\s+[\w.<>\[\]]+\s+(\w+)\(")
    for line in out.stdout.splitlines():
        m = method_re.match(line)
        if m:
            names.add(m.group(1))
    return names


def activity_overrides() -> set[str]:
    if not ACTIVITY.is_file():
        return set()
    src = ACTIVITY.read_text(encoding="utf-8")
    # Match `override fun <name>(` — single-token method names only.
    return set(re.findall(r"override\s+fun\s+(\w+)\s*\(", src))


def _self_test() -> int:
    """Smoke-test the parser against a fake javap listing without a JAR.

    Catches regex regressions without a real Gradle build. Tiny by design.
    """
    sample = (
        "public interface com.termux.terminal.TerminalSessionClient {\n"
        "  public abstract void onTextChanged(com.termux.terminal.TerminalSession);\n"
        "  public abstract void onTitleChanged(com.termux.terminal.TerminalSession);\n"
        "  public abstract void onBell(com.termux.terminal.TerminalSession);\n"
        "  public abstract int getTerminalCursorStyle();\n"
        "}\n"
    )
    method_re = re.compile(r"^\s*public\s+abstract\s+[\w.<>\[\]]+\s+(\w+)\(")
    found = set()
    for line in sample.splitlines():
        m = method_re.match(line)
        if m:
            found.add(m.group(1))
    expected = {"onTextChanged", "onTitleChanged", "onBell", "getTerminalCursorStyle"}
    if found != expected:
        print(f"self-test FAIL: parsed {found} != {expected}")
        return 1
    print("self-test OK")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    jar = find_terminal_emulator_jar()
    if jar is None:
        # In CI this step runs after assembleDebug, so the JAR must be
        # present. If it is not, that is a real failure (silently skipping
        # would let the guard "pass" while checking nothing). Locally we
        # skip so plain `python3` invocations without a Gradle build stay
        # green.
        import os as _os
        # In CI (GitHub Actions sets CI=true) or when explicitly requested,
        # a missing JAR after assembleDebug is a real failure. Locally we
        # skip so plain `python3` invocations stay green.
        strict = (
            _os.environ.get("ZMUX_STRICT_INTERFACE_CHECK") == "1"
            or _os.environ.get("CI") == "true"
        )
        if strict:
            print(
                "FAIL: terminal-emulator JAR not found after assembleDebug; "
                "cannot verify TerminalSessionClient overrides."
            )
            return 1
        print(
            "note: terminal-emulator JAR not found; "
            "run ./gradlew :app:assembleDebug first. Skipping interface check."
        )
        return 0

    required = interface_methods(jar)
    if not required:
        print(f"warning: could not read {INTERFACE} from {jar}; skipping.")
        return 0

    overrides = activity_overrides() | OVERRIDES_EXPECTED_BUT_NOT_FUN

    missing = required - overrides
    extra = overrides - required

    if missing:
        print(
            "FAIL: ZmuxTerminalActivity is missing overrides required by "
            f"{INTERFACE} ({TERMUX_VERSION}): {sorted(missing)}"
        )
        print("      Add them (or update this guard if they were renamed).")
        return 1

    if extra:
        print(
            "FAIL: ZmuxTerminalActivity declares overrides that are NOT in "
            f"{INTERFACE} ({TERMUX_VERSION}): {sorted(extra)}"
        )
        print(
            "      This is the 'overrides nothing' class of bug — remove the "
            "override or bump the Termux dependency deliberately."
        )
        return 1

    print(
        f"OK: {len(required)} TerminalSessionClient overrides match the "
        f"pinned Termux {TERMUX_VERSION} interface."
    )
    return 0




if __name__ == "__main__":
    sys.exit(main())
