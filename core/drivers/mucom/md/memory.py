"""How Mucom-MD reads a song: MUCOM88's image as the PC-88 compiled it, every offset little-endian
from a base the image names (its tracks from +9, its voices from +5).  Its track code holds no
pointer operand: a loop counts back and forward itself (core/rom/grammar/mucom.py)."""

from __future__ import annotations

from core.rom.image import RomError
from core.rom.memory import SoundMemory


class MucomMemory(SoundMemory):
    _BYTE_ORDER = "little"

    def header_pointer(self, header: int, at: int) -> int:
        """The address the offset at `at` names, from `header` (the base the image counts from)."""
        return header + self.word(at)

    def code_pointer(self, operand: int) -> int:
        raise RomError(f"${operand - 1:X}: a pointer operand, which Mucom-MD's track code has none of")
