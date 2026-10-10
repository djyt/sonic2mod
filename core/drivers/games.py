"""Every ROM known by its SHA-1: the game, the driver its songs read with, its data fixes.  A game
is not a driver (MAME's sets beside its drivers): many games share one driver, and naming a ROM
here loads only its driver (registry.py).  A ROM not listed is detected (detect.py).

    Game(sha1, title, driver, fixes)      fixes: a disassembly's data-bug fixes, as byte edits
                                          (core/rom/fixes.py; Sonic 1's FixMusicAndSFXDataBugs)
"""

from __future__ import annotations

from dataclasses import dataclass

from core.rom.fixes import RomFix
from core.rom.image import RomImage

from .names import SmpsDriver


@dataclass(frozen=True)
class Game:
    sha1: str
    title: str
    driver: SmpsDriver
    fixes: tuple[RomFix, ...] = ()


_SONIC1_REV01_FIXES = (
    RomFix(0x754BA, bytes.fromhex("8006C1030306 80B524"), bytes.fromhex("8006B5030306 80A924"),
           "Marble Zone PSG3: nE5 nE5 nE5 / nE4 an octave lower (off the PSG table as shipped)"),
    RomFix(0x781BD, bytes.fromhex("808080E60C"), b"",
           "Credits PSG2: three late rests and an FM-only smpsAlterVol $0C that mutes the passage"),
    RomFix(0x791A0, bytes.fromhex("90"), bytes.fromhex("10"),
           "SndBC Teleport FM5: transposition $90 -> $10"),
)

_GAMES = (
    Game("1f1e480f768237eb0c0e725b622b0d791f47a7a9", "Sonic the Hedgehog (Rev 01)", SmpsDriver.SONIC1, _SONIC1_REV01_FIXES),
    Game("70d9b760c87196af364492512104fa18c9d69cce", "Michael Jackson's Moonwalker (Rev A)", SmpsDriver.TYPE1A),
    Game("2ce17105ca916fbbe3ac9ae3a2086e66b07996dd", "Golden Axe (Rev A)", SmpsDriver.TYPE0FM),
    Game("731cdf182fe647e4977477ba4dd2e2b46b9b878a", "Streets of Rage (Rev A)", SmpsDriver.MUCOM),
)

_BY_SHA1 = {game.sha1: game for game in _GAMES}


def known_game(rom: RomImage) -> Game | None:
    """The game this exact ROM is; None for any other."""
    return _BY_SHA1.get(rom.sha1)


def data_fixes(rom: RomImage) -> tuple[RomFix, ...]:
    """The data fixes known for this exact ROM; none for any other."""
    game = known_game(rom)
    return game.fixes if game else ()
