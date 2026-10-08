"""Every SMPS variant the ROM reader knows, by name, and the ROMs each is known in (by SHA-1)."""

from __future__ import annotations

from core.rom.fixes import RomFix
from core.rom.image import RomImage
from core.rom.variant import SmpsVariant

from .smps68k import MUCOM, SONIC1, TYPE1A
from .smpsz80 import TYPE0FM

DRIVERS = (SONIC1, TYPE1A, TYPE0FM, MUCOM)

VARIANTS = {v.name: v for v in DRIVERS}


def pinned_variant(rom: RomImage) -> SmpsVariant | None:
    """The variant this exact ROM is known to use; None for any other ROM."""
    return next((v for v in VARIANTS.values() if rom.sha1 in v.known_roms), None)


def data_fixes(rom: RomImage) -> tuple[RomFix, ...]:
    """The data fixes known for this exact ROM; none for any other."""
    variant = pinned_variant(rom)
    return variant.known_roms[rom.sha1] if variant else ()
