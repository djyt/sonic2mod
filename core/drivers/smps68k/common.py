"""What the SMPS 68k drivers here share: the header and voice layouts, and the flags they share
byte for byte (each variant adds its own: sonic1.py, type1a.py).  The same byte can mean
different things: $F9 is Type 1a's return and Sonic 1's FM1 release rate, $FB / $FA Type 1a's
transposition / tempo divider, Sonic 1's $E9 / $E5.
docs/todo/binary_import.md has the table.
"""

from __future__ import annotations

from core.chips import OperatorReg
from core.rom.flags import CALL, JUMP, LOOP, NO_ATTACK, STOP, FlagSpec, effect
from core.rom.variant import HeaderLayout, TrackSlot, VoiceLayout
from core.smps import SFX_CHANNEL_IDS, ChannelType, CoordFlag

# The DAC, then FM1-FM6 in header order; SFX on FM3-FM5 and the PSG
HEADER_68K = HeaderLayout((TrackSlot(ChannelType.DAC), *[TrackSlot(ChannelType.FM)] * 6), frozenset(SFX_CHANNEL_IDS.values()))


# 25 bytes: feedback / algorithm, then each register's four operator bytes, TL last
VOICE_68K = VoiceLayout((OperatorReg.DT_MUL, OperatorReg.KS_AR, OperatorReg.AM_D1R, OperatorReg.D2R,
                         OperatorReg.D1L_RR, OperatorReg.TL))

FLAGS_68K: dict[int, FlagSpec] = {
    0xE0: effect(CoordFlag.PAN),
    0xE1: effect(CoordFlag.DETUNE),
    0xE2: effect(CoordFlag.NOP),
    0xE6: effect(CoordFlag.ALTER_VOL),
    0xE7: NO_ATTACK,
    0xE8: effect(CoordFlag.NOTE_FILL),
    0xEC: effect(CoordFlag.ALTER_VOL),      # the PSG twin of $E6: one flag in the song
    0xEF: effect(CoordFlag.SET_VOICE),
    0xF0: effect(CoordFlag.MOD_SET, 4),
    0xF1: effect(CoordFlag.MOD_ON, 0),
    0xF2: STOP,
    0xF3: effect(CoordFlag.PSG_FORM),
    0xF4: effect(CoordFlag.MOD_OFF, 0),
    0xF5: effect(CoordFlag.PSG_VOICE),
    0xF6: JUMP,
    0xF7: LOOP,
    0xF8: CALL,
}
