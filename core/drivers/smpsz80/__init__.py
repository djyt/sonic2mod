"""The SMPS Z80 family: the whole sound driver runs on the Z80, its data little-endian and read
through the Z80's bank window.

    memory.py     pointers: absolute Z80 addresses in the 32 KB bank at $8000
    program.py    the driver: the Z80 program the 68k loads with an FM table, and that table
    type0fm/      one folder per driver

Nothing here imports a driver: each loads on first use (core/drivers/registry.py).
"""
