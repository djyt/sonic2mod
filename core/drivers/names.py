"""Each driver's name: a config's `driver:`, SmpsVariant.name, PlaybackRules.driver."""

from __future__ import annotations

from enum import StrEnum


class SmpsDriver(StrEnum):
    SONIC1 = "sonic1"
    TYPE1A = "smps68k_type1a"      # SMPS 68k Type 1a: Michael Jackson's Moonwalker
    TYPE0FM = "smpsz80_type0fm"    # SMPS Z80 Type 0 FM (an early Type 1 FM): Golden Axe
    STREETS_OF_RAGE = "smps68k_streets_of_rage"  # SMPS 68k Type 1b with MUCOM-style track code: Streets of Rage
    SH2 = "smpsz80_sh2"            # an early SMPS Z80 (Golden Axe's ancestor): Space Harrier II
    OUTRUN = "smpsz80_outrun"      # an SMPS Z80 on Timer A, Sonic 1's flags: OutRun
    MUCOM_MD = "mucom_md"          # Mucom-MD 0.85, MUCOM88's songs on the 68k: Streets of Rage 2
    TIMETRAX = "timetrax"          # Tim Follin's Z80 driver, no SMPS: Time Trax (prototype)


# The driver of a song that states none: an asm song (SMPS2ASM, Sonic 1's disassembly) or a VGM lift
DEFAULT_DRIVER = SmpsDriver.SONIC1
