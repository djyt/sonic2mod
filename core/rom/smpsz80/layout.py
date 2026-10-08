"""How SMPS Z80 Type 0 FM lays out its headers and voices (Golden Axe's driver, Z80 $0501 and
$03E5).

    music tracks   the drum track (its notes trigger drum programs on FM3), FM1 FM2 FM4 FM5 FM6
    tempo          0 never stalls (the counter is never loaded: Death Adder); the counter is loaded as the
                   song starts, after that frame's tempo check ($06CA before $043A): the first hold
                   comes a frame late, at frame m (the rips: every key-on, at modifier 2 too)
    run-out        a note keyed 256 frames without an attacking read is keyed off ($00E8: the fill
                   counter, never set, wraps; core/smps/run_out.py)
    voice          26 bytes: B0, B4 (pan, AMS, FMS), then TL DT/MUL KS/AR AM/D1R D2R D1L/RR
"""

from __future__ import annotations

from ...chips import OperatorReg
from ...smps import SFX_CHANNEL_IDS, ChannelType
from ..variant import HeaderLayout, TrackSlot, VoiceLayout

_SFX_FM6 = 0x06                 # an SFX track's channel byte for FM6 (Sonic 1's SFX stop at FM5)

HEADER_TYPE0 = HeaderLayout(
    fm_slots=(TrackSlot(ChannelType.DAC, "FM3"), TrackSlot(ChannelType.FM, "FM1"), TrackSlot(ChannelType.FM, "FM2"),
              TrackSlot(ChannelType.FM, "FM4"), TrackSlot(ChannelType.FM, "FM5"), TrackSlot(ChannelType.FM, "FM6")),
    sfx_channels=frozenset({*SFX_CHANNEL_IDS.values(), _SFX_FM6}),    # FM3-FM6 (no DAC), the PSG
    never_holds=0,
    tempo_phase=1,
    key_run_out=0x100,
)

VOICE_TYPE0 = VoiceLayout((OperatorReg.TL, OperatorReg.DT_MUL, OperatorReg.KS_AR, OperatorReg.AM_D1R,
                           OperatorReg.D2R, OperatorReg.D1L_RR), pan=True)
