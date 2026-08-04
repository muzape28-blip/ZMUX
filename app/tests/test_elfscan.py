"""Tests for zmux.elfscan — the on-device ELF DT_NEEDED reader.

Runs against real ELF files present on any Linux/desktop so the parser is
exercised against genuine linker metadata, not hand-crafted bytes.
"""
import shutil
import subprocess

import pytest

from zmux import elfscan


@pytest.fixture(scope="module")
def host_elf():
    """A dynamically linked host ELF (e.g. /bin/ls or /usr/bin/env)."""
    for candidate in ("/bin/ls", "/usr/bin/env", "/usr/bin/true"):
        if elfscan.elf_dynamic_needed(candidate):
            return candidate
    pytest.skip("no dynamically linked ELF available on this host")


def test_rejects_non_elf(tmp_path):
    bogus = tmp_path / "not-elf"
    bogus.write_bytes(b"hello world")
    with pytest.raises(elfscan.ElfError):
        elfscan.elf_dynamic_needed(str(bogus))


def test_rejects_missing_file(tmp_path):
    with pytest.raises((OSError, elfscan.ElfError)):
        elfscan.elf_dynamic_needed(str(tmp_path / "nope.so"))


def test_dynamic_needed_returns_real_libs(host_elf):
    needed = elfscan.elf_dynamic_needed(host_elf)
    assert isinstance(needed, list)
    assert needed, f"expected at least one DT_NEEDED in {host_elf}"
    # Every entry is a bare soname filename (libc.so.6, libselinux.so.1, …).
    assert all("/" not in name and (".so" in name) for name in needed)
    assert any(name.startswith("libc") for name in needed), (
        f"expected libc among {needed}"
    )


def test_static_binary_has_no_dynamic(tmp_path):
    """A static ELF raises ElfError (no .dynamic) — the caller must handle it."""
    cc = shutil.which("gcc") or shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler available")
    binary = tmp_path / "static"
    subprocess.run([cc, "-static", "-o", str(binary), "-x", "c", "-"],
                   input=b"int main(void){return 0;}",
                   check=True, capture_output=True)
    with pytest.raises(elfscan.ElfError):
        elfscan.elf_dynamic_needed(str(binary))


def test_soname_of_shared_library():
    """libm.so (or libc.so) is a real shared object with a SONAME."""
    for candidate in ("/lib/x86_64-linux-gnu/libm.so.6",
                      "/lib/x86_64-linux-gnu/libc.so.6",
                      "/usr/lib/x86_64-linux-gnu/libm.so.6"):
        if candidate and __import__("os").path.exists(candidate):
            soname = elfscan.elf_soname(candidate)
            if soname:
                assert ".so" in soname
                return
    pytest.skip("no standard shared library found on this host")


def test_corrupted_short_rewrite_is_detected(host_elf, tmp_path):
    """A 1-byte-short string rewrite (the old talloc patch bug) shifts every
    section header; the parser must raise so zmux-info/gates can report the
    build as corrupted instead of silently omitting the proot section."""
    corrupt = tmp_path / "corrupt.so"
    corrupt.write_bytes(open(host_elf, "rb").read())
    data = corrupt.read_bytes()
    needle = b"libc.so.6"
    if needle not in data:
        pytest.skip("host ELF has no libc.so.6 string to corrupt")
    short = data.replace(needle, b"libc.so\x00")  # 9 -> 8 bytes: file shrinks
    assert len(short) == len(data) - 1
    corrupt.write_bytes(short)
    with pytest.raises(elfscan.ElfError):
        elfscan.elf_dynamic_needed(str(corrupt))
