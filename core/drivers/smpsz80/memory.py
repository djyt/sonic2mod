"""How an SMPS Z80 driver reads its sound data: through the Z80's 32 KB bank window
(core.rom.memory.BankedMemory), its pointers little-endian.  A pointer outside the window, or any
read past the bank, is not the driver's data.
"""

from __future__ import annotations

from core.rom.image import RomError, RomImage
from core.rom.memory import BANK_SIZE, BANK_WINDOW, BankedMemory, SoundMemory


def window_view(image: RomImage, bank: int) -> bytes:
    """Z80 addresses $0000-$FFFF as a driver mapping `bank` reads them: the bank in the window, zeros
    below it (a table that names its bytes by Z80 address: OutRun's DAC samples)."""
    return bytes(BANK_WINDOW) + image.bytes_at(bank, BANK_SIZE)


class BankedZ80Memory(BankedMemory):
    _BYTE_ORDER = "little"


class Z80RamMemory(SoundMemory):
    """The driver's own RAM, $0000-$1FFF as the 68k loaded it (core/rom/z80.py): its tables and
    drum programs.  Addresses are Z80 addresses; pointers absolute, little-endian."""

    _BYTE_ORDER = "little"

    def header_pointer(self, _header: int, at: int) -> int:
        return self.word(at)

    def code_pointer(self, operand: int) -> int:
        target = self.word(operand)
        if not self.contains(target):
            raise RomError(f"Z80 ${operand - 1:04X}: pointer to ${target:04X}, outside Z80 RAM")
        return target
