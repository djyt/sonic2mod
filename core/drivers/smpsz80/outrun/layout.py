"""How OutRun's driver lays out its headers and voices (Z80 $0679 the music loader, $05A0 the voices).

    music tracks   the DAC track, then FM1-FM6 in header order (the init table Z80 $0706: channels
                   6, 0, 1, 2, 4, 5, 6; FM3 a normal channel), then PSG1-PSG3 ($0714)
    tempo          a hold every `tempo` updates, each track's duration counter held one ($0997); 0
                   never holds.  The queue loads a song after that update's tempo check ($01F5:
                   $0997, then $09B0), its tracks read in the same update (the counter at 1, $07F9):
                   the first hold a frame late, at frame m
    voice          25 bytes, SMPS's (Sonic 1's): B0, then DT/MUL KS/AR AM/D1R D2R D1L/RR TL, the
                   operators in SMPS's order (the register list Z80 $05E8: $30 $38 $34 $3C ...).
                   A TL byte's bit 7 marks an operator the track volume is added to ($0D17): the
                   carriers in every voice but $32 (Passing Breeze, Step on Beat), which marks OP1
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.variant import SMPS_VOICE, HeaderLayout, TrackSlot
from core.smps import ChannelType

TEMPO_PHASE = 1                 # the first hold a frame late

HEADER_OUTRUN = HeaderLayout(
    fm_slots=(TrackSlot(ChannelType.DAC), *[TrackSlot(ChannelType.FM)] * 6),
    sfx_channels=frozenset(),         # the SFX are in Z80 RAM: not read (music only)
    never_holds=0,
)

VOICE_OUTRUN = replace(SMPS_VOICE, volume_flags=True)
