"""A Mega Drive ROM image: its header and big-endian reads by address (the 68k sees the cartridge
from address 0, so an address is an offset into the file)."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

# The cartridge header (Sega's layout at $100)
_CONSOLE = slice(0x100, 0x110)       # "SEGA MEGA DRIVE " / "SEGA GENESIS    "
_TITLE = slice(0x150, 0x180)         # the overseas title
_SERIAL = slice(0x180, 0x18E)        # "GM 00004049-01": type, product code, revision
_CONSOLE_MARK = b"SEGA"
_HEADER_END = 0x200

# File names a ROM is read from; `.smd` is interleaved in 16 KB blocks, which this reader does not undo
_ROM_SUFFIXES = (".bin", ".md", ".gen")
_INTERLEAVED_SUFFIX = ".smd"


class RomError(ValueError):
    """The ROM, or the data a pointer in it leads to, is not what the driver would read."""


def is_rom_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in (*_ROM_SUFFIXES, _INTERLEAVED_SUFFIX)


@dataclass(frozen=True)
class RomImage:
    data: bytes

    @classmethod
    def load(cls, path: str | Path) -> RomImage:
        if Path(path).suffix.lower() == _INTERLEAVED_SUFFIX:
            raise RomError(f"{path}: an interleaved .smd dump; convert it to a plain .bin first")
        rom = cls(Path(path).read_bytes())
        if len(rom.data) < _HEADER_END or not rom.data[_CONSOLE].startswith(_CONSOLE_MARK):
            raise RomError(f"{path}: no Mega Drive header (\"SEGA\" at $100)")
        return rom

    @property
    def title(self) -> str:
        return _text(self.data[_TITLE])

    @property
    def serial(self) -> str:
        return _text(self.data[_SERIAL])

    @cached_property
    def sha1(self) -> str:
        """The ROM's identity: a file name says nothing about its revision."""
        return hashlib.sha1(self.data).hexdigest()

    def contains(self, address: int, length: int = 1) -> bool:
        return address >= 0 and address + length <= len(self.data)

    def byte(self, address: int) -> int:
        self._check(address, 1)
        return self.data[address]

    def bytes_at(self, address: int, length: int) -> bytes:
        self._check(address, length)
        return self.data[address:address + length]

    def word(self, address: int) -> int:
        return int.from_bytes(self.bytes_at(address, 2), "big")

    def signed_byte(self, address: int) -> int:
        return int.from_bytes(self.bytes_at(address, 1), "big", signed=True)

    def signed_word(self, address: int) -> int:
        return int.from_bytes(self.bytes_at(address, 2), "big", signed=True)

    def long(self, address: int) -> int:
        return int.from_bytes(self.bytes_at(address, 4), "big")

    def find_all(self, pattern: bytes) -> list[int]:
        """Every address `pattern` starts at."""
        found, at = [], self.data.find(pattern)
        while at >= 0:
            found.append(at)
            at = self.data.find(pattern, at + 1)
        return found

    def _check(self, address: int, length: int) -> None:
        if not self.contains(address, length):
            raise RomError(f"read of {length} byte(s) at ${address:X}: outside the ROM (${len(self.data):X} bytes)")


def _text(raw: bytes) -> str:
    """A header field: Shift-JIS where it is (Golden Axe's full-width "ＧＯＬＤＥＮ ＡＸＥ" -> "GOLDEN AXE"),
    else byte for byte; padded with spaces or NULs."""
    try:
        text = unicodedata.normalize("NFKC", raw.decode("shift_jis"))
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return " ".join(text.replace("\0", " ").split())
