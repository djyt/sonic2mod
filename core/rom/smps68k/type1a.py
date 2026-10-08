"""SMPS 68k Type 1a: Michael Jackson's Moonwalker.  No disassembly: its jump table at $61290, read
with a disassembler (docs/todo/binary_import.md, Phase 2)."""

from __future__ import annotations

from ...smps import CoordFlag, SmpsDriver
from ..flags import RETURN, EnvelopeCommand, drop, effect, refuse
from ..variant import SmpsVariant
from .common import FLAGS_68K, HEADER_68K, VOICE_68K, no_fm_drums, sonic1_fm_frequencies
from .dac import type1a_dac
from .locate import locate_68k
from .memory import Relative68kMemory

MOONWALKER_REV_A_SHA1 = "70d9b760c87196af364492512104fa18c9d69cce"

_LAST_NOTE = 0xDF

TYPE1A = SmpsVariant(
    name=SmpsDriver.TYPE1A,
    memory=Relative68kMemory,
    locate=locate_68k,
    flags={
        **FLAGS_68K,
        0xE3: refuse("sets a global flag ($FC clears it; what reads it is not known)"),
        0xE4: drop("pan animation", 1, more_if_set=4),
        0xE5: refuse("FM and PSG volume in one flag", 2),
        0xE9: refuse("LFO", 2),
        0xEA: effect(CoordFlag.DETUNE),
        0xEB: drop("queued sound (not part of the music)", 1),
        0xED: effect(CoordFlag.DETUNE),
        0xEE: effect(CoordFlag.DETUNE),
        0xF9: RETURN,
        0xFA: effect(CoordFlag.CHAN_TEMPO_DIV),
        0xFB: effect(CoordFlag.CHANGE_TRANSPOSITION),
        0xFC: refuse("clears $E3's global flag"),
        0xFD: refuse("SSG-EG", 4),
        0xFE: refuse("FM3 special mode", 8),
        0xFF: effect(CoordFlag.PAN),                  # past the jump table: runs into $E0's handler
    },
    envelope_commands={0x83: EnvelopeCommand.HOLD, 0x80: EnvelopeCommand.RESTART,
                       0x85: EnvelopeCommand.JUMP},   # $61152
    header=HEADER_68K,
    voice_layout=VOICE_68K,
    # Every note byte goes to the DAC (the 68k remaps $88-$97 to pitched samples)
    dac_names={b: f"dac{b:02X}" for b in range(0x81, _LAST_NOTE + 1)},
    dac=type1a_dac,
    fm_frequencies=sonic1_fm_frequencies,
    fm_drums=no_fm_drums,
    known_roms={MOONWALKER_REV_A_SHA1: ()},
)
