"""Data fixes: the disassembly's FixMusicAndSFXDataBugs, as byte edits to the one ROM each is
known in.  A ROM is matched by its SHA-1, and every edit checks the bytes it replaces: another
game, or another build of this one, gets none of them.

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


SONIC1_REV01_SHA1 = "1f1e480f768237eb0c0e725b622b0d791f47a7a9"

_FIXES: dict[str, tuple[RomFix, ...]] = {
    SONIC1_REV01_SHA1: (
        RomFix(0x754BA, bytes.fromhex("8006C1030306 80B524".replace(" ", "")),
               bytes.fromhex("8006B5030306 80A924".replace(" ", "")),
               "Marble Zone PSG3: nE5 nE5 nE5 / nE4 an octave lower (off the PSG table as shipped)"),
        RomFix(0x781BD, bytes.fromhex("808080E60C"), b"",
               "Credits PSG2: three late rests and an FM-only smpsAlterVol $0C that mutes the passage"),
        RomFix(0x791A0, bytes.fromhex("90"), bytes.fromhex("10"),
               "SndBC Teleport FM5: transposition $90 -> $10"),
    ),
}


def data_fixes(rom: RomImage) -> tuple[RomFix, ...]:
    """The fixes known for this exact ROM; none for any other."""
    return _FIXES.get(rom.sha1, ())


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
