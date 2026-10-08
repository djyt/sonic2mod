"""How the SMPS 68k drivers here read pointers (SonicDriverVer 1): big-endian; a header's pointer
is an offset from the header, a flag's is relative: target = operand + 1 + signed word."""

from __future__ import annotations

from core.rom.image import RomError
from core.rom.memory import SoundMemory


class Relative68kMemory(SoundMemory):
    _BYTE_ORDER = "big"

    def header_pointer(self, header: int, at: int) -> int:
        return header + self.word(at)

    def code_pointer(self, operand: int) -> int:
        target = operand + 1 + self._image.signed_word(operand)
        if not self.contains(target):
            raise RomError(f"${operand - 1:X}: pointer to ${target:X}, outside the ROM")
        return target
