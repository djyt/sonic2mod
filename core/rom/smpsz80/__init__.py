"""The SMPS Z80 family: the whole sound driver runs on the Z80, its data little-endian and read
through the Z80's bank window.

    memory.py     pointers: absolute Z80 addresses in the 32 KB bank at $8000
    locate.py     the driver (its FM table) and the bank (its sound header) -> SoundIndex
"""
