"""A sound driver's view of the ROM: how it reads words and pointers.  Addresses stay ROM offsets;
only a driver's pointer *values* differ (68k: big-endian offsets, relative; Z80: little-endian,
absolute in a bank window).  Readers ask a SoundMemory and never see byte order or pointer rules.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import Literal

from .image import RomImage


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
