"""Streets of Rage 2: Mucom-MD 0.85, MUCOM88's songs on the 68k (docs/todo/streets_of_rage_2.md).

    variant.py    the description: flags per kind of track, rules, tables read from the ROM
    header.py     a song: MUCOM88's compiled image, its tracks' loops
    locate.py     the index and tables, by the code that reads each
    grammar.py    its dialect of MUCOM's language, the $FF prefix
    memory.py     little-endian offsets from the image's bases
"""

from .variant import MUCOM_MD

__all__ = ["MUCOM_MD"]
