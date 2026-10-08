"""The sound drivers the ROM reader knows, each a description the generic readers (core/rom) are
driven by: no reader asks which driver it reads.  Grouped by family, one folder per driver:

    smps68k/            the driver on the 68k: relative big-endian pointers, the Go_ block, DPCM
        sonic1/         Sonic the Hedgehog (Type 1b)
        type1a/         Michael Jackson's Moonwalker (Type 1a)
        mucom/          Streets of Rage (Type 1b, MUCOM-style track code)
    smpsz80/            the driver on the Z80: the bank window, absolute little-endian pointers
        type0fm/        Golden Axe (Type 0 FM)

A family folder holds what its drivers share; a driver folder its description (variant.py) and
what only it has.  Drivers import the framework by absolute path (core.rom...), family code
relatively.  A new driver: docs/smps_variants.md § Adding a variant.
"""

from .smps68k import MUCOM, SONIC1, TYPE1A
from .smpsz80 import TYPE0FM

DRIVERS = (SONIC1, TYPE1A, TYPE0FM, MUCOM)

__all__ = ["DRIVERS"]
