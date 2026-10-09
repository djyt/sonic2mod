"""Streets of Rage's track grammar: SMPS 68k's engine reading MUCOM-style code (docs/todo/
streets_of_rage.md § 1.4).  Each instruction is read into the ops the SMPS walk knows.

    d n        d $01-$7F frames, n octave (bits 4-6) | semitone   -> NOTE, DURATION
    $80|d      rest                                               -> NOTE $80, DURATION
    $00        end of track                                       -> STOP
    $F0-$FF    a flag: the kind's table (variant.py); those SMPS's vocabulary spells otherwise
               are read here (read(...) in the tables)

Notes as SMPS numbers ($81 = C0): FM octave o plays block o; a PSG row r plays octave r + 1 from
row 2 (C3, Sonic 1's PSG table entry 0; rows 0-1 read row 2, rows 6-9 row 6).  A drum track's
note byte is not read: it plays the sample $F0 chose (SELECTED_SAMPLE).

Loops: `$F5` opens one (its body: a label), `$F6 x n back` closes it (LOOP, n passes), `$FE`
leaves it on its last pass (LOOP_EXIT).  The counts live on a per-track stack the walk does not
need: it unrolls each loop from its `$F6`.
"""

from __future__ import annotations

from functools import partial

from core.rom.grammar import Instruction, flag_instruction, track_label
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.rom.variant import SmpsVariant
from core.smps import (
    FIRST_NOTE,
    REST,
    SELECTED_SAMPLE,
    AlterVol,
    ChannelType,
    Detune,
    DetuneAdd,
    ModOff,
    ModOn,
    ModSet,
    Op,
    OpKind,
    Pan,
    PsgForm,
    SelectSample,
    SmpsEffect,
    VoiceRegister,
    signed_byte,
)

_FIRST_FLAG = 0xF0
_BYTE_MASK = 0xFF
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


_LOOP_END_LENGTH = 5               # $F6 x n back.w
_LOOP_END = 0xF6                   # where a loop break lands past
_PSG_DEPTH_SHIFT = 4               # the PSG adds the vibrato word >> 4 to its divider
_DETUNE_SETS = 0                   # $F2's third byte: 0 sets, any other adds
_TIMERS = range(0x24, 0x27)        # Timer A, Timer B: MUCOM's tempo, which nothing here reads
_OPERATOR_REGISTERS = range(0x30, 0xA0)

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


def _effect(effect: SmpsEffect, length: int) -> Instruction:
    return Instruction((Op(OpKind.EFFECT, effect=effect),), length, True)


# --- loops ---------------------------------------------------------------------------------

def loop_start(memory: SoundMemory, address: int) -> Instruction:
    """`$F5 offset.w`: the body starts after it (its count is the closing `$F6`'s)."""
    body = address + 3
    return Instruction((), 3, True, target=body)


def loop_end(memory: SoundMemory, address: int) -> Instruction:
    """`$F6 x n back.w`: n passes of the body that starts `back` before the word's end."""
    count = memory.byte(address + 2)
    body = address + 3 - _le_word(memory, address + 3)
    op = Op(OpKind.LOOP, value=count, name=track_label(body), index=memory.byte(address + 1))
    return Instruction((op,), _LOOP_END_LENGTH, True, target=body)


def loop_exit(memory: SoundMemory, address: int) -> Instruction:
    """`$FE offset.w`: on the last pass, on past the loop's `$F6` (offset from the word's end + 2)."""
    after = address + 5 + _le_word(memory, address + 1)
    end = after - _LOOP_END_LENGTH
    if memory.byte(end) != _LOOP_END:
        raise RomError(f"${address:X}: loop break to ${after:X}, not past a loop's end")
    body = end + 3 - _le_word(memory, end + 3)
    return Instruction((Op(OpKind.LOOP_EXIT, name=track_label(body)),), 3, True)


# --- flags with operands the SMPS vocabulary spells differently ------------------------------

def detune(memory: SoundMemory, address: int) -> Instruction:
    """`$F2 lo hi mode`: the detune word, set (mode 0) or added to; the PSG's shifted to a divider
    as it plays (the PSG's TrackRules.detune_shift)."""
    word = _signed_le_word(memory, address + 1)
    if memory.byte(address + 3) != _DETUNE_SETS:
        return _effect(DetuneAdd(word), 4)
    return _effect(Detune(word), 4)


def register_write(memory: SoundMemory, address: int) -> Instruction:
    """`$FA r v`: an operator register patches the voice; a timer is inert.  Any other is refused."""
    register, value = memory.byte(address + 1), memory.byte(address + 2)
    if register in _TIMERS:
        return Instruction((), 3, True, dropped="timer write")
    if register not in _OPERATOR_REGISTERS:
        raise RomError(f"${address:X}: $FA ${register:02X}: a register write not converted")
    return _effect(VoiceRegister(register, value), 3)


def psg_volume_down(memory: SoundMemory, address: int) -> Instruction:
    """`$FB n` on the PSG: n taken from the attenuation (neg.b, add.b)."""
    return _effect(AlterVol(signed_byte(-memory.byte(address + 1) & _BYTE_MASK)), 2)


def _vibrato(memory: SoundMemory, address: int, shift: int) -> Instruction:
    """`$F4 0 delay speed depth.w count`, `$F4 1` off, `$F4 2` on.  SMPS's modulation, but a half
    cycle is count + 1 steps (the driver tests the count before it counts down)."""
    command = memory.byte(address + 1)
    if command == _VIBRATO_OFF:
        return _effect(ModOff(), 2)
    if command == _VIBRATO_ON:
        return _effect(ModOn(), 2)
    if command != _VIBRATO_SET:
        raise RomError(f"${address:X}: $F4 ${command:02X} (one vibrato field changed): not converted")
    delay, speed = memory.byte(address + 2), memory.byte(address + 3)
    depth = _signed_le_word(memory, address + 4) >> shift
    steps = memory.byte(address + 6) + 1
    return _effect(ModSet(delay, speed, depth, steps), 2 + _VIBRATO_SET_BYTES)


def pan(memory: SoundMemory, address: int) -> Instruction:
    """`$F8 n`: centre, left, right, centre."""
    n = memory.byte(address + 1)
    if n >= len(_PAN_B4):
        raise RomError(f"${address:X}: $F8 ${n:02X} reads past the driver's pan table")
    return _effect(Pan(_PAN_B4[n]), 2)


def noise(memory: SoundMemory, address: int) -> Instruction:
    """`$F7 x` on a PSG track: white noise clocked by tone 3 (the operand is not read)."""
    return _effect(PsgForm(_NOISE_FORM), 2)


def dac_sample(memory: SoundMemory, address: int) -> Instruction:
    """`$F0 n` on the drum track: its notes play sample $80 | n."""
    return _effect(SelectSample(_DAC_SAMPLE_BIT | memory.byte(address + 1)), 2)


# Vibrato: FM words as written; the PSG's >> 4 (a divider, not an FNUM).  Each step's, where the
# driver shifts the sum: Phase 5 checks the depth
fm_vibrato, psg_vibrato = partial(_vibrato, shift=0), partial(_vibrato, shift=_PSG_DEPTH_SHIFT)
