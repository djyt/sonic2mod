"""The SMPS Z80 family: the whole sound driver runs on the Z80, its data little-endian and read
through the Z80's bank window.

    memory.py     pointers: absolute Z80 addresses in the 32 KB bank at $8000
    type0fm/      one folder per driver
"""

from .type0fm import TYPE0FM

__all__ = ["TYPE0FM"]
