"""SMPS 68k Type 1a: Michael Jackson's Moonwalker.  No disassembly: its jump table at $61290, read
with a disassembler (docs/todo/binary_import.md, Phase 2)."""

from __future__ import annotations

from dataclasses import replace

from core.rom.flags import RETURN, EnvelopeCommand, drop, effect, every_kind, refuse
from core.rom.image import RomError, RomImage
from core.rom.variant import SmpsVariant
from core.smps import FIRST_NOTE, LAST_NOTE, CoordFlag, PlaybackRules

from ...names import SmpsDriver
from ...reference import FM_FREQUENCIES, PSG_FREQUENCIES, PSG_FREQUENCIES_EXTENDED
from ..common import FLAGS_68K, HEADER_68K, VOICE_68K
from ..locate import locate_68k
from ..memory import Relative68kMemory
from .dac import type1a_dac

# The PSG table, by the code that reads it ($610C2): andi.w #$7F,d5 / lsl.b #1,d5 / lea (xxx).l,a0
_PSG_TABLE_READ = bytes.fromhex("0245007FE30D41F9")
_PSG_READ_WORDS = 128               # a 7-bit index: the table, then the bytes past it
_PSG_TABLE_LIKE_SONIC = 69          # Sonic 1's entries 0-68 (69, nMaxPSG's 0, stays as Sonic 1's: 1)


def _rules_from_rom(rom: RomImage, rules: PlaybackRules) -> PlaybackRules:
    """The words a PSG note reads past the table: this ROM's, not Sonic 1's (Boss Theme's noise
    track plays index 92: $00CE, as its rip)."""
    found = rom.find_all(_PSG_TABLE_READ)
    if len(found) != 1:
        raise RomError(f"{len(found)} reads of the PSG table, not one: not Moonwalker's driver")
    table = rom.long(found[0] + len(_PSG_TABLE_READ))
    words = tuple(rom.word(table + 2 * i) for i in range(_PSG_READ_WORDS))
    if words[:_PSG_TABLE_LIKE_SONIC] != PSG_FREQUENCIES[:_PSG_TABLE_LIKE_SONIC]:
        raise RomError(f"PSG table ${table:X}: not Sonic 1's")
    return replace(rules, psg_read=PSG_FREQUENCIES + words[len(PSG_FREQUENCIES):])


TYPE1A = SmpsVariant(
    name=SmpsDriver.TYPE1A,
    memory=Relative68kMemory,
    locate=locate_68k,
    flags=every_kind({
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
    }),
    envelope_commands={0x83: EnvelopeCommand.HOLD, 0x80: EnvelopeCommand.RESTART,
                       0x85: EnvelopeCommand.JUMP},   # $61152
    header=HEADER_68K,
    # Sonic 1's voice bytes; but loading a voice ($61402) writes each carrier's TL from the track
    # volume alone ($61478: the mask at $614C0, Sonic 1's), the voice's own never plays
    voice_layout=replace(VOICE_68K, carrier_tl=False),
    # Sonic 1's FM and PSG tables (compared: the same); the words past the PSG table and the PSG
    # envelopes read from the ROM; every note byte goes to the DAC (the 68k remaps $88-$97 to
    # pitched samples)
    rules=PlaybackRules(driver=SmpsDriver.TYPE1A, fm_frequencies=FM_FREQUENCIES, psg_frequencies=PSG_FREQUENCIES,
                        psg_read=PSG_FREQUENCIES_EXTENDED, psg_envelopes={},
                        dac_names={b: f"dac{b:02X}" for b in range(FIRST_NOTE, LAST_NOTE + 1)}),
    rules_from_rom=_rules_from_rom,
    dac=type1a_dac,
)
