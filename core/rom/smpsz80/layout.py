"""How SMPS Z80 Type 0 FM lays out its headers and voices (Golden Axe's driver, Z80 $0501 and
$03E5).

    music tracks   the drum track (its notes trigger drum programs on FM3), FM1 FM2 FM4 FM5 FM6
    tempo          0 never stalls (the counter is never loaded: Death Adder)
    voice          26 bytes: B0, B4 (pan, AMS, FMS), then TL DT/MUL KS/AR AM/D1R D2R D1L/RR
"""

from __future__ import annotations

from ...chips import OperatorReg
from ..variant import HeaderLayout, TrackSlot, VoiceLayout

HEADER_TYPE0 = HeaderLayout(
    fm_slots=(TrackSlot("DAC", "FM3"), TrackSlot("FM", "FM1"), TrackSlot("FM", "FM2"),
              TrackSlot("FM", "FM4"), TrackSlot("FM", "FM5"), TrackSlot("FM", "FM6")),
    sfx_channels=frozenset({0x02, 0x04, 0x05, 0x06, 0x80, 0xA0, 0xC0, 0xE0}),    # FM3-FM6 (no DAC), the PSG
    never_holds=0,
)

VOICE_TYPE0 = VoiceLayout((OperatorReg.TL, OperatorReg.DT_MUL, OperatorReg.KS_AR, OperatorReg.AM_D1R,
                           OperatorReg.D2R, OperatorReg.D1L_RR), pan=True)
