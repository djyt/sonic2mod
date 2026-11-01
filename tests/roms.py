"""The game ROMs and rips some tests read: copyrighted, so not in git (input/roms/, reference/vgz/).
A test that needs one is skipped without it.

GAMES is the one list of them: a new game is a line here (its ROM's SHA-1, driver and fixes are
core/drivers/games.py's).  tests/core/drivers/test_contract.py holds each to the driver contract,
tests/tool_regression.py reads its songs (read_<name>) and, with `frames`, checks its rips
(frames_<name>, glitches_<name>).
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ROMS = _ROOT / "input" / "roms"
_RIPS = _ROOT / "reference" / "vgz"
_CONFIGS = _ROOT / "configs"


@dataclass(frozen=True)
class GameFiles:
    """One game's files here."""
    name: str                   # its cases' names (read_<name>) and its configs' folder
    rom: Path
    rips: Path                  # its VGZ rips
    frames: bool = True         # vgm_frames and its glitch scan run over its rips and configs

    @property
    def configs(self) -> Path:
        return _CONFIGS / self.name


GAMES = (
    # Sonic 1's own cases read its asm as well as its ROM (tool_regression.py _READS); its rips are
    # audited by vgm_compare, not vgm_frames
    GameFiles("sonic_1", _ROMS / "sonic_rev01.bin", _RIPS / "sonic_1", frames=False),
    GameFiles("moonwalker", _ROMS / "Michael Jackson's Moonwalker (World) (Rev A).md", _RIPS / "moonwalker", frames=False),
    GameFiles("golden_axe", _ROMS / "Golden Axe (World) (Rev A).md", _RIPS / "golden_axe"),
    GameFiles("streets_of_rage", _ROMS / "Bare Knuckle - Ikari no Tekken ~ Streets of Rage (World) (Rev A).md",
              _RIPS / "streets_of_rage_1"),
    GameFiles("space_harrier_2", _ROMS / "Space Harrier II (World).md", _RIPS / "space_harrier_2"),
    GameFiles("outrun", _ROMS / "OutRun (USA, Europe).md", _RIPS / "outrun"),
    GameFiles("streets_of_rage_2", _ROMS / "Streets of Rage 2 (USA).md", _RIPS / "streets_of_rage_2"),
)
_BY_NAME = {g.name: g for g in GAMES}

SONIC1_ROM = _BY_NAME["sonic_1"].rom
SONIC1_ASM = _ROOT / "reference" / "smps_drivers" / "sonic_1"
MOONWALKER_ROM = _BY_NAME["moonwalker"].rom
MOONWALKER_RIPS = _BY_NAME["moonwalker"].rips
GOLDEN_AXE_ROM = _BY_NAME["golden_axe"].rom
GOLDEN_AXE_RIPS = _BY_NAME["golden_axe"].rips
STREETS_OF_RAGE_ROM = _BY_NAME["streets_of_rage"].rom
STREETS_OF_RAGE_RIPS = _BY_NAME["streets_of_rage"].rips
SPACE_HARRIER_2_ROM = _BY_NAME["space_harrier_2"].rom
SPACE_HARRIER_2_RIPS = _BY_NAME["space_harrier_2"].rips
OUTRUN_ROM = _BY_NAME["outrun"].rom
OUTRUN_RIPS = _BY_NAME["outrun"].rips
STREETS_OF_RAGE_2_ROM = _BY_NAME["streets_of_rage_2"].rom


def needs(*paths: Path):
    """Skip a test (or a class) unless every path exists."""
    missing = [p.relative_to(_ROOT).as_posix() for p in paths if not p.exists()]
    return unittest.skipUnless(not missing, "needs " + " and ".join(missing))


needs_moonwalker = needs(MOONWALKER_ROM)
needs_moonwalker_rips = needs(MOONWALKER_ROM, MOONWALKER_RIPS)
needs_streets_of_rage = needs(STREETS_OF_RAGE_ROM)
needs_sonic1_rom_and_asm = needs(SONIC1_ROM, SONIC1_ASM)
needs_golden_axe = needs(GOLDEN_AXE_ROM)
needs_golden_axe_rips = needs(GOLDEN_AXE_ROM, GOLDEN_AXE_RIPS)
needs_streets_of_rage_rips = needs(STREETS_OF_RAGE_ROM, STREETS_OF_RAGE_RIPS)
needs_space_harrier_2 = needs(SPACE_HARRIER_2_ROM)
needs_space_harrier_2_rips = needs(SPACE_HARRIER_2_ROM, SPACE_HARRIER_2_RIPS)
needs_outrun = needs(OUTRUN_ROM)
needs_streets_of_rage_2 = needs(STREETS_OF_RAGE_2_ROM)
