"""The game ROMs and rips some tests read: copyrighted, so not in git (input/roms/, reference/vgz/).
A test that needs one is skipped without it."""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SONIC1_ROM = _ROOT / "input" / "roms" / "sonic_rev01.bin"
SONIC1_ASM = _ROOT / "reference" / "smps_drivers" / "sonic_1"
MOONWALKER_ROM = _ROOT / "input" / "roms" / "Michael Jackson's Moonwalker (World) (Rev A).md"
MOONWALKER_RIPS = _ROOT / "reference" / "vgz" / "moonwalker"
GOLDEN_AXE_ROM = _ROOT / "input" / "roms" / "Golden Axe (World) (Rev A).md"
GOLDEN_AXE_RIPS = _ROOT / "reference" / "vgz" / "golden_axe"
STREETS_OF_RAGE_ROM = _ROOT / "input" / "roms" / "Bare Knuckle - Ikari no Tekken ~ Streets of Rage (World) (Rev A).md"
STREETS_OF_RAGE_RIPS = _ROOT / "reference" / "vgz" / "streets_of_rage_1"
SPACE_HARRIER_2_ROM = _ROOT / "input" / "roms" / "Space Harrier II (World).md"
SPACE_HARRIER_2_RIPS = _ROOT / "reference" / "vgz" / "space_harrier_2"


def _needs(*paths: Path):
    """Skip a test (or a class) unless every path exists."""
    missing = [p.relative_to(_ROOT).as_posix() for p in paths if not p.exists()]
    return unittest.skipUnless(not missing, "needs " + " and ".join(missing))


needs_moonwalker = _needs(MOONWALKER_ROM)
needs_moonwalker_rips = _needs(MOONWALKER_ROM, MOONWALKER_RIPS)
needs_streets_of_rage = _needs(STREETS_OF_RAGE_ROM)
needs_sonic1_rom_and_asm = _needs(SONIC1_ROM, SONIC1_ASM)
needs_golden_axe = _needs(GOLDEN_AXE_ROM)
needs_golden_axe_rips = _needs(GOLDEN_AXE_ROM, GOLDEN_AXE_RIPS)
needs_streets_of_rage_rips = _needs(STREETS_OF_RAGE_ROM, STREETS_OF_RAGE_RIPS)
needs_space_harrier_2 = _needs(SPACE_HARRIER_2_ROM)
needs_space_harrier_2_rips = _needs(SPACE_HARRIER_2_ROM, SPACE_HARRIER_2_RIPS)
