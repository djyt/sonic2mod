"""How an SMPS Z80 driver reads its sound data: through the Z80's 32 KB bank window.

    Z80 $8000-$FFFF  ->  ROM bank + (address - $8000)       bank: a multiple of $8000

Pointers are absolute Z80 addresses, little-endian.  A pointer outside the window, or any
read past the bank, is not the driver's data.
"""

from __future__ import annotations

from core.rom.image import RomError, RomImage
from core.rom.memory import SoundMemory

BANK_SIZE = 0x8000
_WINDOW = 0x8000           # the Z80 address the bank appears at


class BankedZ80Memory(SoundMemory):
    _BYTE_ORDER = "little"

    def __init__(self, image: RomImage, bank: int):
        super().__init__(image)
        self._bank = bank

    def contains(self, address: int, length: int = 1) -> bool:
        return self._bank <= address and address + length <= self._bank + BANK_SIZE and super().contains(address, length)

    def header_pointer(self, header: int, at: int) -> int:
        return self._rom_address(self.word(at))

    def code_pointer(self, operand: int) -> int:
        target = self._rom_address(self.word(operand))
        if not self.contains(target):
            raise RomError(f"${operand - 1:X}: pointer to Z80 ${self.word(operand):04X}, outside the bank")
        return target

    def _rom_address(self, z80_address: int) -> int:
        """Where a Z80 window address reads from.  Outside the window: an address outside the bank."""
        return self._bank + z80_address - _WINDOW

    def bytes_at(self, address: int, length: int) -> bytes:
        if not self.contains(address, length):
            raise RomError(f"read of {length} byte(s) at ${address:X}: outside the bank at ${self._bank:X}")
        return super().bytes_at(address, length)

    def byte(self, address: int) -> int:
        return self.bytes_at(address, 1)[0]


class Z80RamMemory(SoundMemory):
    """The driver's own RAM, $0000-$1FFF as the 68k loaded it (core/rom/z80.py): its tables and
    drum programs.  Addresses are Z80 addresses; pointers absolute, little-endian."""

    _BYTE_ORDER = "little"

    def header_pointer(self, header: int, at: int) -> int:
        return self.word(at)

    def code_pointer(self, operand: int) -> int:
        target = self.word(operand)
        if not self.contains(target):
            raise RomError(f"Z80 ${operand - 1:04X}: pointer to ${target:04X}, outside Z80 RAM")
        return target
