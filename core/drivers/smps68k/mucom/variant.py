"""SMPS 68k Type 1b with MUCOM-style track code: Streets of Rage (Bare Knuckle).  No disassembly:
its driver ($72914-$73C16) read with a disassembler (docs/todo/streets_of_rage.md).

    tables      found by the code that reads them (each a lea after the instruction below)
                music index   subi.b #$81,d0 ... lea   $7288C   17 absolute longs from $81
                SFX index     subi.b #$A0,d0 / lea      $73B56   listed, not read (music only)
                envelopes     move.b $B(a3),d0 ... lea  $728D0   5
                FM octave     andi.w #$F,d0 / lea       $732D2   C-B, block = the note's octave
                PSG rows      mulu.w #12,d0 ... lea     $73A64   rows 2-6: Sonic 1's PSG table
    header      voices.w fm.b psg.b, FM entries ptr.w volume.b (FM1-FM5, the drum track),
                PSG entries ptr.w volume.b envelope.b (tone 3, tone 2, tone 1); no tempo: a tick a frame
    voice       25 bytes: DT/MUL TL KS/AR AM/D1R D2R D1L/RR (register order), FB/ALG last
    flags       one table per kind of track (the driver jumps through $73302 for FM and the drum
                track, $73342 for the PSG); the grammar (grammar.py) reads the rest
"""

from __future__ import annotations

from functools import lru_cache, partial

from core.chips import OperatorReg
from core.rom.flags import JUMP, NO_ATTACK, EnvelopeCommand, FlagSpec, drop, effect, refuse
from core.rom.header import is_music_header, read_index
from core.rom.image import RomError, RomImage
from core.rom.variant import EntryLayout, HeaderLayout, SmpsVariant, SoundIndex, TrackSlot, VoiceLayout
from core.smps import ChannelType, CoordFlag, PlaybackRules

from ...names import SmpsDriver
from ...reference import PSG_FREQUENCIES, PSG_FREQUENCIES_EXTENDED
from ..locate import pointers_before_data
from ..memory import Relative68kMemory
from .grammar import mucom_instruction

_LONG, _WORD = 4, 2
_LEA_A0 = bytes.fromhex("41F9")                  # lea (xxx).l,a0
_FIRST_MUSIC, _FIRST_SFX = 0x81, 0xA0
_MAX_SOUNDS = 0x60

# Each table: the instruction before the lea that loads it, and how far after it the lea is
_MUSIC_INDEX = (bytes.fromhex("04000081"), 12)   # subi.b #$81,d0
_SFX_INDEX = (bytes.fromhex("040000A0"), 0)      # subi.b #$A0,d0
_ENVELOPES = (bytes.fromhex("102B000B"), 6)      # move.b $B(a3),d0 (the envelope)
_FM_OCTAVE = (bytes.fromhex("0240000F"), 0)      # andi.w #$F,d0 (the semitone)
_PSG_ROWS = (bytes.fromhex("C0FC000C"), 4)       # mulu.w #12,d0 (the row)

_SEMITONES = 12
_OCTAVES = 8
_BLOCK_SHIFT = 11
_PSG_ROWS_LIKE_SONIC = slice(2 * _SEMITONES, 7 * _SEMITONES)     # rows 2-6: C3-B7

_HEADER = HeaderLayout(
    fm_slots=(*[TrackSlot(ChannelType.FM, f"FM{n}") for n in range(1, 6)], TrackSlot(ChannelType.DAC)),
    sfx_channels=frozenset(),
    tempo=False,
    fm_entry=EntryLayout(3, volume=2),
    psg_entry=EntryLayout(4, volume=2, envelope=3),
    psg_slots=("PSG3", "PSG2", "PSG1"),
)

_VOICE_LAYOUT = VoiceLayout((OperatorReg.DT_MUL, OperatorReg.TL, OperatorReg.KS_AR, OperatorReg.AM_D1R,
                           OperatorReg.D2R, OperatorReg.D1L_RR),
                          feedback_last=True, operator_offsets=(0x00, 0x04, 0x08, 0x0C))

# Read and left out for now: volume, gate, register writes, LFO, FM3 special mode
_FM_FLAGS: dict[int, FlagSpec] = {
    0xF0: effect(CoordFlag.SET_VOICE),
    0xF1: drop("volume", 1),
    0xF3: drop("gate", 1),
    0xF7: drop("FM3 special mode", 4),
    0xF9: refuse("pause toggle"),
    0xFA: drop("register write", 2),
    0xFB: drop("volume step", 1),
    0xFC: drop("LFO", 3),
    0xFD: NO_ATTACK,
    0xFF: JUMP,
}

_PSG_FLAGS: dict[int, FlagSpec] = {
    0xF0: drop("$F0 (no PSG effect)", 1),
    0xF1: drop("volume", 1),
    0xF3: drop("gate", 1),
    0xF8: drop("$F8 (no PSG effect)", 1),
    0xF9: effect(CoordFlag.PSG_VOICE),
    0xFA: effect(CoordFlag.PSG_VOICE),
    0xFB: drop("volume step", 1),
    0xFC: refuse("$FC on a PSG track (hangs the driver)"),
    0xFD: NO_ATTACK,
    0xFF: JUMP,
}

_DAC_FLAGS: dict[int, FlagSpec] = {
    0xF1: drop("$F1 (no DAC effect)", 1),
    0xF3: drop("gate", 1),
    0xF7: refuse("$F7 on the drum track"),
    0xF9: refuse("pause toggle"),
    0xFA: drop("register write", 2),
    0xFB: drop("$FB (no DAC effect)", 1),
    0xFC: drop("LFO", 3),
    0xFD: NO_ATTACK,
    0xFF: JUMP,
}


@lru_cache(maxsize=8)
def _locate(rom: RomImage) -> SoundIndex:
    """The song and SFX indexes and the envelopes, by the code that reads each."""
    _check_psg_rows(rom)
    memory = Relative68kMemory(rom)
    music_table = _table(rom, *_MUSIC_INDEX)
    slots = range(music_table, music_table + _MAX_SOUNDS * _LONG, _LONG)
    music = read_index(memory, slots, rom.long, _FIRST_MUSIC, partial(is_music_header, layout=_HEADER))
    sfx = pointers_before_data(rom, _table(rom, *_SFX_INDEX))
    envelopes = pointers_before_data(rom, _table(rom, *_ENVELOPES))
    return SoundIndex(music, {_FIRST_SFX + i: a for i, a in enumerate(sfx)}, envelopes)


def _fm_frequencies(rom: RomImage) -> tuple[int, ...]:
    """The FM frequency words by fm_note_index (1 = C0): the octave's words at blocks 0-7."""
    octave = [rom.word(_table(rom, *_FM_OCTAVE) + i * _WORD) for i in range(_SEMITONES)]
    return (0, *[block << _BLOCK_SHIFT | word for block in range(_OCTAVES) for word in octave])


def _table(rom: RomImage, anchor: bytes, gap: int) -> int:
    """The table the one lea `gap` bytes after `anchor` loads."""
    found = [a for a in rom.find_all(anchor) if rom.bytes_at(a + len(anchor) + gap, _WORD) == _LEA_A0]
    if len(found) != 1:
        raise RomError(f"{len(found)} reads of a table after ${anchor.hex().upper()}, not one: not a Streets of Rage driver")
    return rom.long(found[0] + len(anchor) + gap + _WORD)


def _check_psg_rows(rom: RomImage) -> None:
    """The grammar names PSG notes by Sonic 1's table: rows 2-6 must be its."""
    table = _table(rom, *_PSG_ROWS)
    rows = tuple(rom.word(table + i * _WORD) for i in range(_PSG_ROWS_LIKE_SONIC.start, _PSG_ROWS_LIKE_SONIC.stop))
    if rows != tuple(PSG_FREQUENCIES[:len(rows)]):
        raise RomError(f"PSG table ${table:X}: rows 2-6 are not Sonic 1's")


MUCOM = SmpsVariant(
    name=SmpsDriver.MUCOM,
    memory=Relative68kMemory,
    locate=_locate,
    flags={ChannelType.FM: _FM_FLAGS, ChannelType.PSG: _PSG_FLAGS, ChannelType.DAC: _DAC_FLAGS},
    envelope_commands={0x81: EnvelopeCommand.HOLD, 0x80: EnvelopeCommand.RESTART, 0x83: EnvelopeCommand.MUTE},
    header=_HEADER,
    voice_layout=_VOICE_LAYOUT,
    # Its FM octave and envelopes read from the ROM; Sonic 1's PSG rows (checked); Z80 $019B: 17 samples
    rules=PlaybackRules(driver=SmpsDriver.MUCOM, fm_frequencies=(), psg_frequencies=PSG_FREQUENCIES,
                        psg_read=PSG_FREQUENCIES_EXTENDED, psg_envelopes={},
                        dac_names={b: f"dac{b:02X}" for b in range(0x81, 0x92)}),
    fm_frequencies=_fm_frequencies,
    grammar=mucom_instruction,
)
