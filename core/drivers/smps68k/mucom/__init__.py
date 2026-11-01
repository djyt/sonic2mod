"""Streets of Rage: SMPS 68k Type 1b with MUCOM-style track code (docs/smps_variants.md).

    variant.py    the description: tables found by the code that reads them, layouts, flags per kind
    grammar.py    its dialect of MUCOM's language (core/rom/grammar/mucom.py): PSG rows, pan, PSG flags
"""

from .variant import MUCOM

__all__ = ["MUCOM"]
