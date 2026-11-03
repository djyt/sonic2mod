"""Streets of Rage's track grammar: MUCOM's language (core/rom/grammar/mucom.py) as SMPS 68k's
engine reads it (docs/drivers/streets_of_rage.md).  Its dialect and its own PSG flags:

    PSG notes   row r plays octave r + 1 from row 2 (C3, Sonic 1's PSG table entry 0); rows 0-1
                read row 2, rows 6-9 row 6, past row 9 the table ends
    $F8 n       pan: centre, left, right, centre ($7353A)
    $F4         a half cycle is count + 1 steps
    $FB n       on the PSG: n taken from the attenuation
    $F7 x       on the PSG: white noise clocked by tone 3
"""

from __future__ import annotations

from core.rom.grammar import Instruction, as_instruction
from core.rom.grammar.mucom import SEMITONES, MucomDialect, octave_and_semitone
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.smps import FIRST_NOTE, AlterVol, PsgForm, signed_byte

_BYTE_MASK = 0xFF

# PSG rows: the driver's table repeats row 2 below it and row 6 above, to row 9
_PSG_ROW_C3 = 2
_PSG_TOP_ROW = 6
_PSG_LAST_ROW = 9

_PAN_B4 = (0xC0, 0x80, 0x40, 0xC0)          # $7353A: $F8 n -> B4's speakers (0, 3 centre; 1 left; 2 right)
_NOISE_FORM = 0xE7                           # the driver writes $E7: white noise at tone 3's rate
_VIBRATO_STEPS = 1                           # a half cycle: count + 1 steps


def _psg_note(memory: SoundMemory, at: int) -> int:
    """A PSG note byte as its SMPS number: its row (the whole high nibble) clamped to the rows
    Sonic 1's table holds."""
    _, semitone = octave_and_semitone(memory, at)
    row = memory.byte(at) >> 4
    if row > _PSG_LAST_ROW:
        raise RomError(f"${at:X}: PSG note ${memory.byte(at):02X}: row {row} reads past the driver's PSG table")
    return FIRST_NOTE + SEMITONES * (min(max(row, _PSG_ROW_C3), _PSG_TOP_ROW) - _PSG_ROW_C3) + semitone


DIALECT = MucomDialect(psg_note=_psg_note, pan_b4=_PAN_B4, vibrato_steps=_VIBRATO_STEPS)


def psg_volume_down(memory: SoundMemory, address: int) -> Instruction:
    """`$FB n` on the PSG: n taken from the attenuation (neg.b, add.b)."""
    return as_instruction(AlterVol(signed_byte(-memory.byte(address + 1) & _BYTE_MASK)), 2)


def noise(_memory: SoundMemory, _address: int) -> Instruction:
    """`$F7 x` on a PSG track: white noise clocked by tone 3 (the operand is not read)."""
    return as_instruction(PsgForm(_NOISE_FORM), 2)
