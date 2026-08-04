"""Minimal ELF reader for on-device diagnostics.

ZMUX ships native binaries (libproot.so, libtalloc.so) whose load-time
behavior depends on their ``DT_NEEDED`` entries and SONAME. Android's linker
matches ``DT_NEEDED`` against exact filenames, so a stale binary that still
asks for ``libtalloc.so.2`` fails with

    CANNOT LINK EXECUTABLE ...: library "libtalloc.so.2" not found

even when ``libtalloc.so`` is present. This module reads those strings
directly from the ELF (pure Python, little/big-endian, 32/64-bit) so
``gates`` can prove on the phone itself whether the installed binary is the
fixed one — no adb, no readelf, no guessing.
"""
from __future__ import annotations

import struct

DT_NEEDED = 1
SHT_STRTAB = 3
SHT_DYNAMIC = 6


class ElfError(ValueError):
    """Raised when the file is not a readable ELF of a supported class."""


def _unpack(format_spec: str, data: bytes, offset: int):
    size = struct.calcsize(format_spec)
    return struct.unpack_from(format_spec, data, offset), size


def elf_dynamic_needed(path) -> list:
    """Return the DT_NEEDED library names recorded by an ELF file.

    Parses the section header table, resolves section names through the
    section-header string table, reads ``.dynamic`` and ``.dynstr``, and
    decodes every ``DT_NEEDED`` string. Works for ELF32/ELF64, LE/BE.
    Raises :class:`ElfError` for anything that is not a well-formed ELF.
    """
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:4] != b"\x7fELF":
        raise ElfError(f"{path}: not an ELF file")

    elf_class = data[4]  # 1 = ELF32, 2 = ELF64
    endian = "<" if data[5] == 1 else ">"  # 1 = LSB, 2 = MSB
    if elf_class not in (1, 2):
        raise ElfError(f"{path}: unsupported ELF class {elf_class}")

    if elf_class == 1:  # ELF32
        e_shoff = struct.unpack_from(f"{endian}I", data, 0x20)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
            f"{endian}HHH", data, 0x2E
        )
    else:  # ELF64
        e_shoff = struct.unpack_from(f"{endian}Q", data, 0x28)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
            f"{endian}HHH", data, 0x3A
        )

    if not e_shoff or not e_shnum or not e_shentsize:
        raise ElfError(f"{path}: no section header table")

    def section_header(index: int):
        offset = e_shoff + index * e_shentsize
        if elf_class == 1:
            name, sec_type, _, _, sec_offset, size = struct.unpack_from(
                f"{endian}IIIIII", data, offset
            )
        else:
            name, sec_type = struct.unpack_from(f"{endian}II", data, offset)
            # ELF64 shdr: sh_name(4) sh_type(4) sh_flags(8) sh_addr(8)
            # sh_offset(8) sh_size(8) ... — offset/size sit at +0x18/+0x20.
            sec_offset, size = struct.unpack_from(
                f"{endian}QQ", data, offset + 0x18
            )
        return name, sec_type, sec_offset, size

    def section_name(shstrtab_off: int, shstrtab_size: int, name_idx: int) -> str:
        start = shstrtab_off + name_idx
        end = data.index(b"\x00", start, shstrtab_off + shstrtab_size)
        return data[start:end].decode("utf-8", errors="replace")

    # Resolve the section-header string table first.
    shstr_name, shstr_type, shstr_off, shstr_size = section_header(e_shstrndx)
    if shstr_type != SHT_STRTAB:
        raise ElfError(f"{path}: bad section-header string table")

    dynstr_off = dynstr_size = dynamic_off = dynamic_size = None
    for index in range(e_shnum):
        name_idx, sec_type, sec_offset, size = section_header(index)
        if index == e_shstrndx:
            continue
        name = section_name(shstr_off, shstr_size, name_idx)
        if sec_type == SHT_DYNAMIC and dynamic_off is None:
            dynamic_off, dynamic_size = sec_offset, size
        elif name == ".dynstr" and sec_type == SHT_STRTAB:
            dynstr_off, dynstr_size = sec_offset, size

    if dynamic_off is None or dynstr_off is None:
        raise ElfError(f"{path}: no .dynamic/.dynstr sections (static ELF?)")

    needed: list = []
    if elf_class == 1:
        step = 8
    else:
        step = 16
    for entry in range(0, dynamic_size, step):
        d_tag, d_val = struct.unpack_from(f"{endian}iI", data, dynamic_off + entry) \
            if elf_class == 1 else \
            struct.unpack_from(f"{endian}qQ", data, dynamic_off + entry)
        if d_tag == 0:  # DT_NULL
            break
        if d_tag != DT_NEEDED:
            continue
        start = dynstr_off + d_val
        if start >= dynstr_off + dynstr_size:
            continue
        end = data.index(b"\x00", start, dynstr_off + dynstr_size)
        needed.append(data[start:end].decode("utf-8", errors="replace"))
    return needed


def elf_soname(path) -> str | None:
    """Return the DT_SONAME of an ELF shared object, or None."""
    try:
        return _elf_dynamic_string(path, 0x0E)  # DT_SONAME = 14
    except ElfError:
        return None


def _elf_dynamic_string(path, wanted_tag: int) -> str | None:
    """Decode a single string-valued .dynamic tag (SONAME/NEEDED helper)."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] != b"\x7fELF":
        raise ElfError(f"{path}: not an ELF file")
    elf_class = data[4]
    endian = "<" if data[5] == 1 else ">"
    if elf_class == 1:
        e_shoff = struct.unpack_from(f"{endian}I", data, 0x20)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
            f"{endian}HHH", data, 0x2E
        )
    else:
        e_shoff = struct.unpack_from(f"{endian}Q", data, 0x28)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
            f"{endian}HHH", data, 0x3A
        )

    def section_header(index: int):
        offset = e_shoff + index * e_shentsize
        if elf_class == 1:
            name, sec_type, _, _, sec_offset, size = struct.unpack_from(
                f"{endian}IIIIII", data, offset
            )
        else:
            name, sec_type = struct.unpack_from(f"{endian}II", data, offset)
            sec_offset, size = struct.unpack_from(
                f"{endian}QQ", data, offset + 0x18
            )
        return name, sec_type, sec_offset, size

    def section_name(shstrtab_off, shstrtab_size, name_idx):
        start = shstrtab_off + name_idx
        end = data.index(b"\x00", start, shstrtab_off + shstrtab_size)
        return data[start:end].decode("utf-8", errors="replace")

    shstr_name, shstr_type, shstr_off, shstr_size = section_header(e_shstrndx)
    dynstr_off = dynstr_size = dynamic_off = dynamic_size = None
    for index in range(e_shnum):
        name_idx, sec_type, sec_offset, size = section_header(index)
        if index == e_shstrndx:
            continue
        name = section_name(shstr_off, shstr_size, name_idx)
        if sec_type == SHT_DYNAMIC and dynamic_off is None:
            dynamic_off, dynamic_size = sec_offset, size
        elif name == ".dynstr" and sec_type == SHT_STRTAB:
            dynstr_off, dynstr_size = sec_offset, size
    if dynamic_off is None or dynstr_off is None:
        return None
    step = 8 if elf_class == 1 else 16
    for entry in range(0, dynamic_size, step):
        if elf_class == 1:
            d_tag, d_val = struct.unpack_from(f"{endian}iI", data, dynamic_off + entry)
        else:
            d_tag, d_val = struct.unpack_from(f"{endian}qQ", data, dynamic_off + entry)
        if d_tag == 0:
            break
        if d_tag != wanted_tag:
            continue
        start = dynstr_off + d_val
        end = data.index(b"\x00", start, dynstr_off + dynstr_size)
        return data[start:end].decode("utf-8", errors="replace")
    return None
