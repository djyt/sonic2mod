"""A sound driver's view of the ROM: how it reads words and pointers.  Addresses stay ROM offsets;
only a driver's pointer *values* differ (68k: big-endian offsets, relative; Z80: absolute in a bank
window, little-endian, or big-endian where the driver reads them so: Time Trax's).  Readers ask a
SoundMemory and never see byte order or pointer rules.

    Z80 $8000-$FFFF  ->  ROM bank + (address - $8000)       BankedMemory; bank: a multiple of $8000
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import Literal

from .image import RomError, RomImage

BANK_SIZE = 0x8000
BANK_WINDOW = 0x8000       # the Z80 address the bank appears at


class SoundMemory(ABC):
    _BYTE_ORDER: Literal["big", "little"]     # 68k / Z80

    def __init__(self, image: RomImage):
        self._image = image

    def patched(self, address: int, replacement: bytes) -> SoundMemory:
        """This view with `replacement` read from `address` on (a data fix's bytes)."""
        data = self._image.data
        view = copy.copy(self)
        view._image = RomImage(data[:address] + replacement + data[address + len(replacement):])
        return view

    def contains(self, address: int, length: int = 1) -> bool:
        return self._image.contains(address, length)

    def byte(self, address: int) -> int:
        return self._image.byte(address)

    def bytes_at(self, address: int, length: int) -> bytes:
        return self._image.bytes_at(address, length)

    def word(self, address: int) -> int:
        """A 16-bit field in the driver's byte order."""
        return int.from_bytes(self.bytes_at(address, 2), self._BYTE_ORDER)

    @abstractmethod
    def header_pointer(self, header: int, at: int) -> int:
        """The address a song header's pointer field at `at` names (`header`: the header's own)."""

    @abstractmethod
    def code_pointer(self, operand: int) -> int:
        """The address a jump / loop / call operand at `operand` names; RomError when outside."""


class BankedMemory(SoundMemory):
    """A Z80 driver's data, read through its 32 KB bank window: pointers are absolute Z80 addresses,
    in the driver's byte order (a subclass's).  A pointer outside the window, or any read past the
    bank, is not the driver's data."""

    def __init__(self, image: RomImage, bank: int):
        super().__init__(image)
        self._bank = bank

    def contains(self, address: int, length: int = 1) -> bool:
        return self._bank <= address and address + length <= self._bank + BANK_SIZE and super().contains(address, length)

    def header_pointer(self, _header: int, at: int) -> int:
        return self.rom_address(self.word(at))

    def code_pointer(self, operand: int) -> int:
        target = self.rom_address(self.word(operand))
        if not self.contains(target):
            raise RomError(f"${operand - 1:X}: pointer to Z80 ${self.word(operand):04X}, outside the bank")
        return target

    def rom_address(self, z80_address: int) -> int:
        """Where a Z80 window address reads from.  Outside the window: an address outside the bank."""
        return self._bank + z80_address - BANK_WINDOW

    def bytes_at(self, address: int, length: int) -> bytes:
        if not self.contains(address, length):
            raise RomError(f"read of {length} byte(s) at ${address:X}: outside the bank at ${self._bank:X}")
        return super().bytes_at(address, length)

    def byte(self, address: int) -> int:
        return self.bytes_at(address, 1)[0]
