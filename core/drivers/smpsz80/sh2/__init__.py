"""Space Harrier II: an early SMPS Z80 (Golden Axe's ancestor).

    variant.py    the description: flags, the drum track
    locate.py     the bank and the tables, by the code that reads them -> SoundIndex
    memory.py     the bank with the tables: a song's tempo, a voice's list
    header.py     a song's track list -> its header
    voices.py     register lists -> voices
"""

from .variant import SH2

__all__ = ["SH2"]
