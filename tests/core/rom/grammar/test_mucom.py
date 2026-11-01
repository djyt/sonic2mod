"""MUCOM's track grammar (core/rom/grammar/mucom.py) on hand-built bytes, read in a dialect of test
data: notes, rests, the end, loops, detune, register writes, pan, vibrato, the drum track's sample.

    python -m pytest tests/core/rom/grammar/test_mucom.py -q
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.streets_of_rage import STREETS_OF_RAGE
from core.rom import RomError, RomImage
from core.rom.flags import every_kind, read
from core.rom.grammar import Instruction, track_label
from core.rom.grammar.mucom import (
    MucomDialect,
    dac_sample,
    detune,
    loop_end,
    loop_exit,
    loop_start,
    register_write,
)
from core.rom.memory import SoundMemory
from core.smps import (
    FIRST_NOTE,
    REST,
    SELECTED_SAMPLE,
    ChannelType,
    Detune,
    DetuneAdd,
    ModOff,
    ModSet,
    OpKind,
    Pan,
    SelectSample,
    VoiceRegister,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_AT = 0x200                     # where the hand-built bytes start
_PSG_BASE = 0x90                # the test dialect's PSG notes: this + the byte's low nibble


def _psg_note(memory: SoundMemory, at: int) -> int:
    return _PSG_BASE + (memory.byte(at) & 0xF)


_DIALECT = MucomDialect(psg_note=_psg_note, pan_b4=(0x00, 0x40, 0x80), vibrato_steps=0)
_FLAGS = {0xF0: read(dac_sample), 0xF2: read(detune), 0xF4: read(_DIALECT.vibrato), 0xF5: read(loop_start),
          0xF6: read(loop_end), 0xF8: read(_DIALECT.pan), 0xFA: read(register_write), 0xFE: read(loop_exit)}
_VARIANT = replace(STREETS_OF_RAGE, name="test", flags=every_kind(_FLAGS), grammar=_DIALECT.instruction)


def _memory(code: bytes) -> Relative68kMemory:
    return Relative68kMemory(RomImage(_HEADER + code))


def _read(code: bytes, kind: ChannelType = ChannelType.FM, at: int = 0) -> Instruction:
    return _DIALECT.instruction(_memory(code), _AT + at, _VARIANT, kind)


def _effect(code: bytes, kind: ChannelType = ChannelType.FM):
    one = _read(code, kind)
    assert one.ops[0].kind is OpKind.EFFECT
    return one.ops[0].effect, one.length


class Notes(unittest.TestCase):
    def test_fm_note_is_octave_and_semitone(self):
        one = _read(bytes([0x06, 0x45]))
        self.assertEqual([(op.kind, op.value) for op in one.ops],
                         [(OpKind.NOTE, FIRST_NOTE + 12 * 4 + 5), (OpKind.DURATION, 6)])
        self.assertEqual((one.length, one.falls_through), (2, True))

    def test_psg_note_is_the_dialects(self):
        self.assertEqual(_read(bytes([0x06, 0x47]), ChannelType.PSG).ops[0].value, _PSG_BASE + 7)

    def test_drum_note_plays_the_selected_sample(self):
        self.assertEqual(_read(bytes([0x06, 0x45]), ChannelType.DAC).ops[0].value, SELECTED_SAMPLE)

    def test_rest_and_end(self):
        rest = _read(bytes([0x83]))
        self.assertEqual([(op.kind, op.value) for op in rest.ops], [(OpKind.NOTE, REST), (OpKind.DURATION, 3)])
        self.assertEqual(rest.length, 1)
        end = _read(bytes([0x00]))
        self.assertEqual((end.ops[0].kind, end.falls_through), (OpKind.STOP, False))

    def test_semitone_past_b_is_refused(self):
        with self.assertRaises(RomError):
            _read(bytes([0x06, 0x4C]))


class Loops(unittest.TestCase):
    # $F5 o.w | body: a note | $F6 x n back.w | $FE sits in the body: $F5 ... $FE o.w ... $F6 ...
    _CODE = bytes([0xF5, 0, 0, 0x06, 0x45, 0xFE, 5, 0, 0x06, 0x47, 0xF6, 0, 3, 10, 0])

    def test_start_opens_the_body(self):
        one = _read(self._CODE)
        self.assertEqual((one.ops, one.length, one.target), ((), 3, _AT + 3))

    def test_end_loops_back_n_passes(self):
        one = _read(self._CODE, at=10)
        op = one.ops[0]
        self.assertEqual((op.kind, op.value, op.name, one.length), (OpKind.LOOP, 3, track_label(_AT + 3), 5))

    def test_exit_names_the_loop_it_leaves(self):
        one = _read(self._CODE, at=5)
        self.assertEqual((one.ops[0].kind, one.ops[0].name, one.length), (OpKind.LOOP_EXIT, track_label(_AT + 3), 3))

    def test_exit_not_past_a_loop_end_is_refused(self):
        with self.assertRaises(RomError):
            _read(bytes([0xFE, 0, 0, 0, 0, 0, 0, 0]))


class Flags(unittest.TestCase):
    def test_detune_sets_or_adds_a_signed_word(self):
        self.assertEqual(_effect(bytes([0xF2, 0xFE, 0xFF, 0])), (Detune(-2), 4))
        self.assertEqual(_effect(bytes([0xF2, 0x03, 0x00, 1])), (DetuneAdd(3), 4))

    def test_register_write(self):
        self.assertEqual(_effect(bytes([0xFA, 0x44, 0x20])), (VoiceRegister(0x44, 0x20), 3))
        timer = _read(bytes([0xFA, 0x26, 0x10]))
        self.assertEqual((timer.ops, timer.dropped), ((), "timer write"))
        with self.assertRaises(RomError):
            _read(bytes([0xFA, 0x28, 0xF0]))

    def test_pan_by_the_dialects_table(self):
        self.assertEqual(_effect(bytes([0xF8, 1])), (Pan(0x40), 2))
        with self.assertRaises(RomError):
            _read(bytes([0xF8, 3]))

    def test_vibrato_steps_by_the_dialect(self):
        self.assertEqual(_effect(bytes([0xF4, 0, 2, 1, 0xFC, 0xFF, 5])), (ModSet(2, 1, -4, 5), 7))
        bias = replace(_DIALECT, vibrato_steps=1)
        self.assertEqual(bias.vibrato(_memory(bytes([0xF4, 0, 2, 1, 4, 0, 5])), _AT).ops[0].effect, ModSet(2, 1, 4, 6))
        self.assertEqual(_effect(bytes([0xF4, 1])), (ModOff(), 2))
        with self.assertRaises(RomError):
            _read(bytes([0xF4, 3, 0]))

    def test_drum_sample(self):
        self.assertEqual(_effect(bytes([0xF0, 2]), ChannelType.DAC), (SelectSample(0x82), 2))


if __name__ == "__main__":
    unittest.main()
