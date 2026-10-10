"""Each driver's name: a config's `driver:`, SmpsVariant.name, PlaybackRules.driver."""

from __future__ import annotations

from enum import StrEnum


class SmpsDriver(StrEnum):
    SONIC1 = "sonic1"
    TYPE1A = "smps68k_type1a"      # SMPS 68k Type 1a: Michael Jackson's Moonwalker
    TYPE0FM = "smpsz80_type0fm"    # SMPS Z80 Type 0 FM (an early Type 1 FM): Golden Axe
    MUCOM = "smps68k_mucom"        # SMPS 68k Type 1b with MUCOM-style track code: Streets of Rage
    SH2 = "smpsz80_sh2"            # an early SMPS Z80 (Golden Axe's ancestor): Space Harrier II


# The driver of a song that states none: an asm song (SMPS2ASM, Sonic 1's disassembly) or a VGM lift
DEFAULT_DRIVER = SmpsDriver.SONIC1
