"""Streets of Rage 2's DAC samples: Streets of Rage's player, rewritten (core/rom/pcm/sor.py).

    68k   copied raw to Z80 RAM ($40DA2; core/rom/z80.py: copied)
    Z80   ld iy,$01BF: $81-$84 (the drum track's $80 + n, n 1-3 in every song); $85 reads the first
          sample's bytes (unused); from $86 the voice clips, banked (not read: music only)
    loop  cycles per output ($00EE-$0185): a nibble 186 (high) / 246 (low, the byte's loop in it), a
          run's step 219, each + 13 a pitch step; a run's entry 19 (high) / 79 (low) more.  The 68k's
          stall: Phase 4.2 (from the rips), none until then
"""

from __future__ import annotations

from collections.abc import Mapping

from core.rom.image import RomImage
from core.rom.pcm.sor import RunDpcm, player_samples
from core.rom.variant import DacSample
from core.rom.z80 import z80_ram

MUSIC_SAMPLES = range(0x81, 0x85)
RUN_DPCM = RunDpcm(nibble=(186, 246), run_step=219, run_entry=(19, 79), per_pitch=13, stall=0.0)


def mucom_md_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """The samples a Streets of Rage 2 song can play ($81-$84)."""
    return player_samples(z80_ram(rom), RUN_DPCM, names, MUSIC_SAMPLES)
