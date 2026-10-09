"""Streets of Rage's track grammar: SMPS 68k's engine reading MUCOM-style code (docs/todo/
streets_of_rage.md § 1.4).  Each instruction is read into the ops the SMPS walk knows.

    d n        d $01-$7F frames, n octave (bits 4-6) | semitone   -> NOTE, DURATION
    $80|d      rest                                               -> NOTE $80, DURATION
    $00        end of track                                       -> STOP
    $F0-$FF    a flag: the kind's table (variant.py), or a handler here

Notes as SMPS numbers ($81 = C0): FM octave o plays block o; a PSG row r plays octave r + 1 from
row 2 (C3, Sonic 1's PSG table entry 0; rows 0-1 read row 2, rows 6-9 row 6).  A drum track's
note byte is not read: it plays the sample $F0 chose (SELECTED_SAMPLE).

Loops: `$F5` opens one (its body: a label), `$F6 x n back` closes it (LOOP, n passes), `$FE`
leaves it on its last pass (LOOP_EXIT).  The counts live on a per-track stack the walk does not
need: it unrolls each loop from its `$F6`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from core.rom.grammar import Instruction, flag_instruction, track_label
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.rom.variant import SmpsVariant
from core.smps import (
    FIRST_NOTE,
    REST,
    SELECTED_SAMPLE,
    ChannelType,
    CoordFlag,
    Op,
    OpKind,
    SmpsEffect,
)

_FIRST_FLAG = 0xF0
_END = 0x00
_REST_BIT = 0x80
_DURATION_BITS = 0x7F

_SEMITONES = 12
_OCTAVE_BITS = 0x70
_SEMITONE_BITS = 0x0F

# PSG rows: the driver's table repeats row 2 below it and row 6 above, to row 9
_PSG_ROW_C3 = 2
_PSG_TOP_ROW = 6
_PSG_LAST_ROW = 9

_LOOP_START, _LOOP_END, _LOOP_EXIT = 0xF5, 0xF6, 0xFE
_DETUNE, _VIBRATO, _PAN = 0xF2, 0xF4, 0xF8
_FORM = 0xF7                       # on the PSG: noise
_SAMPLE = 0xF0                     # on the drum track: the sample its notes play

_LOOP_END_LENGTH = 5               # $F6 x n back.w
_PSG_DETUNE_SHIFT = 4              # the PSG adds the word >> 4 to its divider
_DETUNE_SETS = 0                   # $F2's third byte: 0 sets, any other adds

_PAN_B4 = (0xC0, 0x80, 0x40, 0xC0)          # $7353A: $F8 n -> B4's speakers (0, 3 centre; 1 left; 2 right)
_NOISE_FORM = 0xE7                           # the driver writes $E7: white noise at tone 3's rate
_DAC_SAMPLE_BIT = 0x80                       # $F0 n plays sample $80 | n

# $F4's sub-commands: set (5 operand bytes), off, on; 3-6 change one field (no song uses them)
_VIBRATO_SET, _VIBRATO_OFF, _VIBRATO_ON = 0, 1, 2
_VIBRATO_SET_BYTES = 5


def mucom_instruction(memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType) -> Instruction:
    """The instruction at `address`, read by a `kind` track."""
    byte = memory.byte(address)
    if byte == _END:
        return Instruction((Op(OpKind.STOP),), 1, False)
    if byte & _REST_BIT and byte < _FIRST_FLAG:
        return Instruction((Op(OpKind.NOTE, value=REST), Op(OpKind.DURATION, value=byte & _DURATION_BITS)), 1, True)
    if byte < _FIRST_FLAG:
        note = Op(OpKind.NOTE, value=_note(memory, address + 1, kind))
        return Instruction((note, Op(OpKind.DURATION, value=byte)), 2, True)

    handler = _HANDLERS[kind].get(byte) or _COMMON.get(byte)
    if handler is not None:
        return handler(memory, address)
    return flag_instruction(memory, address, variant, kind)


def _note(memory: SoundMemory, at: int, kind: ChannelType) -> int:
    """A note byte as its SMPS number."""
    if kind == ChannelType.DAC:
        return SELECTED_SAMPLE
    byte = memory.byte(at)
    semitone = byte & _SEMITONE_BITS
    if semitone >= _SEMITONES:
        raise RomError(f"${at:X}: note ${byte:02X}: semitone {semitone} reads past the driver's octave table")
    if kind == ChannelType.FM:
        return FIRST_NOTE + _SEMITONES * ((byte & _OCTAVE_BITS) >> 4) + semitone

    row = byte >> 4
    if row > _PSG_LAST_ROW:
        raise RomError(f"${at:X}: PSG note ${byte:02X}: row {row} reads past the driver's PSG table")
    return FIRST_NOTE + _SEMITONES * (min(max(row, _PSG_ROW_C3), _PSG_TOP_ROW) - _PSG_ROW_C3) + semitone


def _le_word(memory: SoundMemory, at: int) -> int:
    return memory.byte(at) | memory.byte(at + 1) << 8


def _signed_le_word(memory: SoundMemory, at: int) -> int:
    word = _le_word(memory, at)
    return word - 0x10000 if word & 0x8000 else word


def _effect(flag: CoordFlag, params: list, length: int) -> Instruction:
    return Instruction((Op(OpKind.EFFECT, effect=SmpsEffect(flag, params)),), length, True)


# --- loops ---------------------------------------------------------------------------------

def _loop_start(memory: SoundMemory, address: int) -> Instruction:
    """`$F5 offset.w`: the body starts after it (its count is the closing `$F6`'s)."""
    body = address + 3
    return Instruction((), 3, True, target=body)


def _loop_end(memory: SoundMemory, address: int) -> Instruction:
    """`$F6 x n back.w`: n passes of the body that starts `back` before the word's end."""
    count = memory.byte(address + 2)
    body = address + 3 - _le_word(memory, address + 3)
    op = Op(OpKind.LOOP, value=count, name=track_label(body), index=memory.byte(address + 1))
    return Instruction((op,), _LOOP_END_LENGTH, True, target=body)


def _loop_exit(memory: SoundMemory, address: int) -> Instruction:
    """`$FE offset.w`: on the last pass, on past the loop's `$F6` (offset from the word's end + 2)."""
    after = address + 5 + _le_word(memory, address + 1)
    end = after - _LOOP_END_LENGTH
    if memory.byte(end) != _LOOP_END:
        raise RomError(f"${address:X}: loop break to ${after:X}, not past a loop's end")
    body = end + 3 - _le_word(memory, end + 3)
    return Instruction((Op(OpKind.LOOP_EXIT, name=track_label(body)),), 3, True)


# --- flags with operands the SMPS vocabulary spells differently ------------------------------

def _detune(memory: SoundMemory, address: int, shift: int) -> Instruction:
    """`$F2 lo hi mode`: an FNUM (PSG: divider) offset, set; one that adds is not read yet."""
    if memory.byte(address + 3) != _DETUNE_SETS:
        return Instruction((), 4, True, dropped="detune that adds")
    return _effect(CoordFlag.DETUNE, [_signed_le_word(memory, address + 1) >> shift], 4)


def _vibrato(memory: SoundMemory, address: int, shift: int) -> Instruction:
    """`$F4 0 delay speed depth.w count`, `$F4 1` off, `$F4 2` on.  SMPS's modulation, but a half
    cycle is count + 1 steps (the driver tests the count before it counts down)."""
    command = memory.byte(address + 1)
    if command == _VIBRATO_OFF:
        return _effect(CoordFlag.MOD_OFF, [], 2)
    if command == _VIBRATO_ON:
        return _effect(CoordFlag.MOD_ON, [], 2)
    if command != _VIBRATO_SET:
        raise RomError(f"${address:X}: $F4 ${command:02X} (one vibrato field changed): not converted")
    delay, speed = memory.byte(address + 2), memory.byte(address + 3)
    depth = _signed_le_word(memory, address + 4) >> shift
    steps = memory.byte(address + 6) + 1
    return _effect(CoordFlag.MOD_SET, [delay, speed, depth, steps], 2 + _VIBRATO_SET_BYTES)


def _pan(memory: SoundMemory, address: int) -> Instruction:
    """`$F8 n`: centre, left, right, centre."""
    n = memory.byte(address + 1)
    if n >= len(_PAN_B4):
        raise RomError(f"${address:X}: $F8 ${n:02X} reads past the driver's pan table")
    return _effect(CoordFlag.PAN, [_PAN_B4[n]], 2)


def _noise(memory: SoundMemory, address: int) -> Instruction:
    """`$F7 x` on a PSG track: white noise clocked by tone 3 (the operand is not read)."""
    return _effect(CoordFlag.PSG_FORM, [_NOISE_FORM], 2)


def _dac_sample(memory: SoundMemory, address: int) -> Instruction:
    """`$F0 n` on the drum track: its notes play sample $80 | n."""
    return _effect(CoordFlag.DAC_SAMPLE, [_DAC_SAMPLE_BIT | memory.byte(address + 1)], 2)


_Handler = Callable[[SoundMemory, int], Instruction]

_COMMON: dict[int, _Handler] = {_LOOP_START: _loop_start, _LOOP_END: _loop_end, _LOOP_EXIT: _loop_exit}

# FM words as written; the PSG's >> 4 (a divider, not an FNUM)
_FM_DETUNE, _PSG_DETUNE = partial(_detune, shift=0), partial(_detune, shift=_PSG_DETUNE_SHIFT)
_FM_VIBRATO, _PSG_VIBRATO = partial(_vibrato, shift=0), partial(_vibrato, shift=_PSG_DETUNE_SHIFT)

_HANDLERS: dict[ChannelType, dict[int, _Handler]] = {
    ChannelType.FM: {_DETUNE: _FM_DETUNE, _VIBRATO: _FM_VIBRATO, _PAN: _pan},
    ChannelType.PSG: {_DETUNE: _PSG_DETUNE, _VIBRATO: _PSG_VIBRATO, _FORM: _noise},
    ChannelType.DAC: {_DETUNE: _FM_DETUNE, _VIBRATO: _FM_VIBRATO, _PAN: _pan, _SAMPLE: _dac_sample},
}
