"""The SMPS Z80 family: the whole sound driver runs on the Z80, its data little-endian and read
through the Z80's bank window.

    type0fm.py    Type 0 FM (Golden Axe): flags, the drum track
    layout.py     its header (track order, tempo 0) and 26-byte voice
    memory.py     pointers: absolute Z80 addresses in the 32 KB bank at $8000
    locate.py     the driver (its FM table) and the bank (its sound header) -> SoundIndex
    drums.py      the drum track's FM drum programs, run frame by frame
"""

from .type0fm import TYPE0FM

__all__ = ["TYPE0FM"]
