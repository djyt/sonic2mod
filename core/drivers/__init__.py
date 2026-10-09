"""The sound drivers songs are read with, a layer above the ROM framework (core/rom): each driver a
description the framework's readers are driven by, so no reader asks which driver it reads.
Grouped by family, one folder per driver:

    smps68k/            the driver on the 68k: relative big-endian pointers, the Go_ block, DPCM
        sonic1/         Sonic the Hedgehog (Type 1b)
        type1a/         Michael Jackson's Moonwalker (Type 1a)
        mucom/          Streets of Rage (Type 1b, MUCOM-style track code)
    smpsz80/            the driver on the Z80: the bank window, absolute little-endian pointers
        type0fm/        Golden Axe (Type 0 FM)
    registry.py         every driver by name, loaded on first use
    games.py            every ROM known by its SHA-1: its game, driver and data fixes
    detect.py           the driver a ROM's songs read with: pinned, else the one that reads them all
    read.py             what a ROM holds: its index, each sound -> SongCode / SmpsSong, its DAC samples

    source  ->  drivers (registry / detect / read  <-  families  <-  drivers)  ->  rom  ->  smps

A family folder holds what its drivers share; a driver folder its description (variant.py) and
what only it has.  Drivers import the framework by absolute path (core.rom...), their family
relatively.  A new driver: docs/smps_variants.md § Adding a variant.
"""

from .detect import detect_variant, first_failure
from .games import data_fixes
from .read import dac_samples, locate_sounds, read_rom_code, read_rom_song
from .registry import load_driver

__all__ = [
    "dac_samples",
    "data_fixes",
    "detect_variant",
    "first_failure",
    "load_driver",
    "locate_sounds",
    "read_rom_code",
    "read_rom_song",
]
