"""The game ROMs and rips some tests read: copyrighted, so not in git (input/roms/, reference/vgz/).
A test that needs one is skipped without it."""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
MOONWALKER_ROM = _ROOT / "input" / "roms" / "Michael Jackson's Moonwalker (World) (Rev A).md"
MOONWALKER_RIPS = _ROOT / "reference" / "vgz" / "moonwalker"


def _needs(*paths: Path):
    """Skip a test (or a class) unless every path exists."""
    missing = [p.relative_to(_ROOT).as_posix() for p in paths if not p.exists()]
    return unittest.skipUnless(not missing, "needs " + " and ".join(missing))


needs_moonwalker = _needs(MOONWALKER_ROM)
needs_moonwalker_rips = _needs(MOONWALKER_ROM, MOONWALKER_RIPS)
