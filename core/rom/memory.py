"""A sound driver's view of the ROM: how it reads words and pointers.  Addresses stay ROM offsets;
only a driver's pointer *values* differ (68k: big-endian offsets, relative; Z80: little-endian,
absolute in a bank window).  Readers ask a SoundMemory and never see byte order or pointer rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .image import RomImage


class SoundMemory(ABC):
    def __init__(self, image: RomImage):
        self._image = image

    def contains(self, address: int, length: int = 1) -> bool:
        return self._image.contains(address, length)

    def byte(self, address: int) -> int:
        return self._image.byte(address)

    def bytes_at(self, address: int, length: int) -> bytes:
        return self._image.bytes_at(address, length)

    @abstractmethod
    def word(self, address: int) -> int:
        """A 16-bit field in the driver's byte order."""

    @abstractmethod
    def header_pointer(self, header: int, at: int) -> int:
        """The address a song header's pointer field at `at` names (`header`: the header's own)."""

    @abstractmethod
    def code_pointer(self, operand: int) -> int:
        """The address a jump / loop / call operand at `operand` names; RomError when outside."""
