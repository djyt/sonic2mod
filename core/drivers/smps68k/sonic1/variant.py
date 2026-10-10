"""Sonic 1's driver: SMPS 68k Type 1b, modified (s1.sounddriver.asm coordflagLookup)."""

from __future__ import annotations

from core.rom.flags import RETURN, STOP, EnvelopeCommand, drop, effect, every_kind
from core.rom.variant import SmpsVariant
from core.smps import CoordFlag

from ...names import SmpsDriver
from ...reference import SONIC1_RULES
from ..common import FLAGS_68K, HEADER_68K, VOICE_68K
from ..locate import locate_68k
from ..memory import Relative68kMemory
from .dac import sonic1_dac

SONIC1 = SmpsVariant(
    name=SmpsDriver.SONIC1,
    memory=Relative68kMemory,
    locate=locate_68k,
    flags=every_kind({
        **FLAGS_68K,
        0xE3: RETURN,
        0xE4: STOP,                                   # smpsFade: the 1-Up jingle restores the song
        0xE5: effect(CoordFlag.CHAN_TEMPO_DIV),
        0xE9: effect(CoordFlag.CHANGE_TRANSPOSITION),
        0xEA: effect(CoordFlag.SET_TEMPO_MOD),
        0xEB: effect(CoordFlag.SET_TEMPO_DIV),
        0xED: drop("smpsClearPush"),
        0xEE: STOP,                                   # smpsStopSpecial: FM4 handed back to the music
        0xF9: drop("smpsMaxRelRate"),
    }),
    envelope_commands={0x80: EnvelopeCommand.HOLD},
    header=HEADER_68K,
    voice_layout=VOICE_68K,
    rules=SONIC1_RULES,
    dac=sonic1_dac,
)
