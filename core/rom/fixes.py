"""Data fixes: a disassembly's data-bug fixes (Sonic 1's FixMusicAndSFXDataBugs), as byte edits to
the one ROM each is known in (core/drivers/games.py: a game by SHA-1, with its fixes); every
edit checks the bytes it replaces.

    same length     laid over the image before anything is read (headers, notes)
    other length    spliced by the track decoder: the original bytes read as the replacement
                    (Credits' three rests and its smpsAlterVol, deleted)
"""

from __future__ import annotations

from dataclasses import dataclass

from .image import RomError, RomImage


@dataclass(frozen=True)
class RomFix:
    address: int
    original: bytes
    replacement: bytes
    what: str

    @property
    def same_length(self) -> bool:
        return len(self.original) == len(self.replacement)


def apply_fixes(rom: RomImage, fixes: tuple[RomFix, ...]) -> tuple[RomImage, dict[int, RomFix]]:
    """The image with the same-length fixes laid over it, and the splices left for the decoder
    (by address).  A fix whose original bytes the ROM does not hold is refused."""
    data = bytearray(rom.data)
    for fix in fixes:
        if rom.bytes_at(fix.address, len(fix.original)) != fix.original:
            raise RomError(f"data fix at ${fix.address:X} ({fix.what}): the ROM holds other bytes")
        if fix.same_length:
            data[fix.address:fix.address + len(fix.original)] = fix.replacement
    splices = {fix.address: fix for fix in fixes if not fix.same_length}
    return RomImage(bytes(data)), splices
