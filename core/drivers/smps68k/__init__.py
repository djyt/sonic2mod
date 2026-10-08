"""The SMPS 68k family: the driver runs on the 68k, its data big-endian and in the 68k's address
space.

    common.py     the flags, header and voice layout Sonic 1 and Type 1a share
    memory.py     pointers: big-endian, relative (SonicDriverVer 1)
    locate.py     the Go_ block, found by its tables' shape -> SoundIndex
    dpcm.py       the DPCM samples in a driver's Z80 code
    sonic1/  type1a/  mucom/      one folder per driver
"""

from .mucom import MUCOM
from .sonic1 import SONIC1
from .type1a import TYPE1A

__all__ = ["MUCOM", "SONIC1", "TYPE1A"]
