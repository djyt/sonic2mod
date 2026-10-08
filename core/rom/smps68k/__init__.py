"""The SMPS 68k family: the driver runs on the 68k, its data big-endian and in the 68k's address
space.

    common.py     the flags and voice layout every variant here shares
    sonic1.py     Sonic 1 (Type 1b, modified) and rev01's data fixes
    type1a.py     Type 1a (Michael Jackson's Moonwalker)
    mucom.py      Type 1b with MUCOM-style track code (Streets of Rage); mucom_grammar.py its grammar
    memory.py     pointers: big-endian, relative (SonicDriverVer 1)
    locate.py     the Go_ block, found by its tables' shape -> SoundIndex
    dac.py        the DPCM samples in each driver's Z80 code (core/rom/z80.py loads it)
"""

from .mucom import MUCOM
from .sonic1 import SONIC1
from .type1a import TYPE1A

__all__ = ["MUCOM", "SONIC1", "TYPE1A"]
