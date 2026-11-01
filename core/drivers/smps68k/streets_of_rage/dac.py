"""Streets of Rage's DAC samples.

    68k   Kosinski into 68k RAM, then copied to Z80 RAM (core/rom/z80.py: buffered)
    Z80   ld iy,$019B: 5 bytes a sample              start.w, size.w, pitch.b (little-endian)
          $81-$85 play from it; $85 is empty: what the drum track's rest and gate write
          (TrackRules.rest_cuts); from $86 the voice clips (not read: music only)
    data  core.rom.pcm.sor.RunDpcm: the sample's own delta table, nibble 0 a run
    loop  cycles per output: a nibble 190 (high) / 242 (low), a run's step 219, each + 13 a pitch
          step; a run's entry 23 (high) / 75 (low) more.  The rips play pitch 1 at the count
          ($82, $84: 229.0 counted, 229.4-229.6 played), higher pitches 1-3 % fast (their
          emulator's djnz, as Sonic 1's); the 68k holds the Z80's bus 1.6 % of each frame
"""

from __future__ import annotations

from collections.abc import Mapping

from core.rom.image import RomImage
from core.rom.pcm.sor import RunDpcm, player_samples
from core.rom.variant import DacSample
from core.rom.z80 import z80_ram

_MUSIC = range(0x81, 0x85)

# Its player's loop: a literal nibble (high, low), a run's step, a run's entry (high, low), per
# pitch step; the 68k's share of each frame (11.6 of 735 rip samples)
RUN_DPCM = RunDpcm(nibble=(190, 242), run_step=219, run_entry=(23, 75), per_pitch=13, stall=0.016)


def streets_of_rage_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """The samples a Streets of Rage song can play ($81-$84)."""
    return player_samples(z80_ram(rom), RUN_DPCM, names, _MUSIC)
