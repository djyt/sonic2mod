"""Streets of Rage: SMPS 68k Type 1b with MUCOM-style track code (docs/smps_variants.md).

    variant.py    the description: tables found by the code that reads them, layouts, flags per kind
    grammar.py    the track grammar: duration-first notes, PSG rows, [ / ]n loops
"""

from .variant import MUCOM

__all__ = ["MUCOM"]
