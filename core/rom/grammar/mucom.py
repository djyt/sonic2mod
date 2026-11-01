"""MUCOM's track grammar: the language MUCOM88's songs are compiled to, read by Streets of Rage's
driver (SMPS 68k's engine) and Streets of Rage 2's (Mucom-MD) alike.  Each instruction is read into
the ops the SMPS walk knows.

    d n        d $01-$7F frames, n octave (bits 4-6) | semitone   -> NOTE, DURATION
    $80|d      rest                                               -> NOTE $80, DURATION
    $00        end of track                                       -> STOP
    $F0-$FF    a flag: the kind's flag table (the driver's); those SMPS's vocabulary spells
               otherwise are read here (read(...) in the tables)

Notes as SMPS numbers ($81 = C0): FM octave o plays block o; a PSG note as the driver's table
reads it (MucomDialect.psg_note).  A drum track's note byte is not read: it plays the sample `$F0`
chose (SELECTED_SAMPLE).

Loops: `$F5` opens one (its body: a label), `$F6 x n back` closes it (LOOP, n passes), `$FE`
leaves it on its last pass (LOOP_EXIT).  The counts live on a per-track stack the walk does not
need: it unrolls each loop from its `$F6`.  Every offset is little-endian (the PC-88's).

What differs per driver is data, its MucomDialect: PSG note decoding, the pan table, the vibrato's
steps per half cycle.

    Streets of Rage      core/drivers/smps68k/streets_of_rage/grammar.py
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...smps import (
    FIRST_NOTE,
    REST,
    SELECTED_SAMPLE,
    ChannelType,
    Detune,
    DetuneAdd,
    ModOff,
    ModOn,
    ModSet,
    Op,
    OpKind,
    Pan,
    SelectSample,
    SmpsEffect,
    VoiceRegister,
)
from ..image import RomError
from ..memory import SoundMemory
from . import Instruction, flag_instruction, track_label

if TYPE_CHECKING:
    from ..variant import SmpsVariant

_FIRST_FLAG = 0xF0
_END = 0x00
_REST_BIT = 0x80
_DURATION_BITS = 0x7F

SEMITONES = 12
_OCTAVE_BITS = 0x70
_SEMITONE_BITS = 0x0F

_LOOP_END_LENGTH = 5               # $F6 x n back.w
_LOOP_END = 0xF6                   # where a loop break lands past
_DETUNE_SETS = 0                   # $F2's third byte: 0 sets, any other adds
_TIMERS = range(0x24, 0x27)        # Timer A, Timer B: MUCOM's tempo, which nothing here reads
_OPERATOR_REGISTERS = range(0x30, 0xA0)
_DAC_SAMPLE_BIT = 0x80             # $F0 n plays sample $80 | n

# $F4's sub-commands: set (5 operand bytes), off, on; 3-6 change one field (no song uses them)
_VIBRATO_SET, _VIBRATO_OFF, _VIBRATO_ON = 0, 1, 2
_VIBRATO_SET_BYTES = 5


def octave_and_semitone(memory: SoundMemory, at: int) -> tuple[int, int]:
    """A note byte's octave and semitone; a semitone past B reads past every driver's octave table."""
    byte = memory.byte(at)
    semitone = byte & _SEMITONE_BITS
    if semitone >= SEMITONES:
        raise RomError(f"${at:X}: note ${byte:02X}: semitone {semitone} reads past the driver's octave table")
    return (byte & _OCTAVE_BITS) >> 4, semitone


@dataclass(frozen=True)
class MucomDialect:
    """What a driver reads MUCOM's language by: its PSG notes (the note byte at an address -> its
    SMPS number), `$F8`'s pan (B4's speaker bits by operand) and the vibrato's steps per half cycle
    (`$F4 0`'s count + `vibrato_steps`)."""

    psg_note: Callable[[SoundMemory, int], int]
    pan_b4: tuple[int, ...]
    vibrato_steps: int

    def instruction(self, memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType) -> Instruction:
        """The instruction at `address`, read by a `kind` track (SmpsVariant.grammar)."""
        byte = memory.byte(address)
        if byte == _END:
            return Instruction((Op(OpKind.STOP),), 1, False)
        if byte & _REST_BIT and byte < _FIRST_FLAG:
            return Instruction((Op(OpKind.NOTE, value=REST), Op(OpKind.DURATION, value=byte & _DURATION_BITS)), 1, True)
        if byte < _FIRST_FLAG:
            note = Op(OpKind.NOTE, value=self._note(memory, address + 1, kind))
            return Instruction((note, Op(OpKind.DURATION, value=byte)), 2, True)
        return flag_instruction(memory, address, variant, kind)

    def _note(self, memory: SoundMemory, at: int, kind: ChannelType) -> int:
        """A note byte as its SMPS number."""
        if kind == ChannelType.DAC:
            return SELECTED_SAMPLE
        if kind == ChannelType.PSG:
            return self.psg_note(memory, at)
        octave, semitone = octave_and_semitone(memory, at)
        return FIRST_NOTE + SEMITONES * octave + semitone

    def pan(self, memory: SoundMemory, address: int) -> Instruction:
        """`$F8 n`: B4's speakers by the driver's table."""
        n = memory.byte(address + 1)
        if n >= len(self.pan_b4):
            raise RomError(f"${address:X}: $F8 ${n:02X} reads past the driver's pan table")
        return as_instruction(Pan(self.pan_b4[n]), 2)

    def vibrato(self, memory: SoundMemory, address: int) -> Instruction:
        """`$F4 0 delay speed depth.w count`, `$F4 1` off, `$F4 2` on: SMPS's modulation, its steps
        per half cycle the count + the dialect's `vibrato_steps`."""
        command = memory.byte(address + 1)
        if command == _VIBRATO_OFF:
            return as_instruction(ModOff(), 2)
        if command == _VIBRATO_ON:
            return as_instruction(ModOn(), 2)
        if command != _VIBRATO_SET:
            raise RomError(f"${address:X}: $F4 ${command:02X} (one vibrato field changed): not converted")
        delay, speed = memory.byte(address + 2), memory.byte(address + 3)
        depth = signed_le_word(memory, address + 4)
        steps = memory.byte(address + 6) + self.vibrato_steps
        return as_instruction(ModSet(delay, speed, depth, steps), 2 + _VIBRATO_SET_BYTES)


def le_word(memory: SoundMemory, at: int) -> int:
    return memory.byte(at) | memory.byte(at + 1) << 8


def signed_le_word(memory: SoundMemory, at: int) -> int:
    word = le_word(memory, at)
    return word - 0x10000 if word & 0x8000 else word


def as_instruction(e: SmpsEffect, length: int) -> Instruction:
    """An instruction of `length` bytes that is the effect `e`."""
    return Instruction((Op(OpKind.EFFECT, effect=e),), length, True)


# --- loops ---------------------------------------------------------------------------------

def loop_start(_memory: SoundMemory, address: int) -> Instruction:
    """`$F5 offset.w`: the body starts after it (its count is the closing `$F6`'s)."""
    body = address + 3
    return Instruction((), 3, True, target=body)


def loop_end(memory: SoundMemory, address: int) -> Instruction:
    """`$F6 x n back.w`: n passes of the body that starts `back` before the word's end."""
    count = memory.byte(address + 2)
    body = address + 3 - le_word(memory, address + 3)
    op = Op(OpKind.LOOP, value=count, name=track_label(body), index=memory.byte(address + 1))
    return Instruction((op,), _LOOP_END_LENGTH, True, target=body)


def loop_exit(memory: SoundMemory, address: int) -> Instruction:
    """`$FE offset.w`: on the last pass, on past the loop's `$F6` (offset from the word's end + 2)."""
    after = address + 5 + le_word(memory, address + 1)
    end = after - _LOOP_END_LENGTH
    if memory.byte(end) != _LOOP_END:
        raise RomError(f"${address:X}: loop break to ${after:X}, not past a loop's end")
    body = end + 3 - le_word(memory, end + 3)
    return Instruction((Op(OpKind.LOOP_EXIT, name=track_label(body)),), 3, True)


# --- flags with operands the SMPS vocabulary spells differently ------------------------------

def detune(memory: SoundMemory, address: int) -> Instruction:
    """`$F2 lo hi mode`: the detune word, set (mode 0) or added to; the PSG's shifted to a divider
    as it plays (the PSG's TrackRules.word_shift)."""
    word = signed_le_word(memory, address + 1)
    if memory.byte(address + 3) != _DETUNE_SETS:
        return as_instruction(DetuneAdd(word), 4)
    return as_instruction(Detune(word), 4)


def register_write(memory: SoundMemory, address: int) -> Instruction:
    """`$FA r v`: an operator register patches the voice; a timer is inert.  Any other is refused."""
    register, value = memory.byte(address + 1), memory.byte(address + 2)
    if register in _TIMERS:
        return Instruction((), 3, True, dropped="timer write")
    if register not in _OPERATOR_REGISTERS:
        raise RomError(f"${address:X}: $FA ${register:02X}: a register write not converted")
    return as_instruction(VoiceRegister(register, value), 3)


def dac_sample(memory: SoundMemory, address: int) -> Instruction:
    """`$F0 n` on the drum track: its notes play sample $80 | n."""
    return as_instruction(SelectSample(_DAC_SAMPLE_BIT | memory.byte(address + 1)), 2)
