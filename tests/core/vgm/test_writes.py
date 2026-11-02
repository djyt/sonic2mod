"""Register writes decoded one by one (core/vgm/writes.py).

    python -m pytest tests/core/vgm/test_writes.py -q
"""

from __future__ import annotations

import unittest

from core.vgm import VgmLog, VgmOp, VgmWrite, decoded_writes
from core.vgm.reader import VgmHeader
from core.vgm.writes import tone_register

_HEADER = VgmHeader(version=0x150, fm_clock=7670453, psg_clock=3579545, rate=60, total_samples=0, loop_samples=0)


def _decoded(*writes: tuple[VgmOp, int, int, int]) -> list[tuple[str, str]]:
    log = VgmLog(_HEADER, [VgmWrite(i, op, port, reg, value) for i, (op, port, reg, value) in enumerate(writes)])
    return [(w.channel, w.text) for w in decoded_writes(log)]


def _fm(port: int, reg: int, value: int) -> tuple[VgmOp, int, int, int]:
    return VgmOp.FM, port, reg, value


def _psg(byte: int) -> tuple[VgmOp, int, int, int]:
    return VgmOp.PSG, 0, 0, byte


class Fm(unittest.TestCase):
    def test_an_operator_register_by_field_operator_and_channel(self):
        # $46 on part II: TL, the slot at +4 (OP3), channel 3 of the part: FM6
        self.assertEqual(_decoded(_fm(1, 0x46, 0x16)), [("FM6", "TL op3 $16")])

    def test_keys_by_their_channel(self):
        self.assertEqual(_decoded(_fm(0, 0x28, 0xF2), _fm(0, 0x28, 0x04), _fm(0, 0x28, 0x31)),
                         [("FM3", "key on"), ("FM4", "key off"), ("FM2", "key on slots 3")])

    def test_the_chips_own_registers_and_no_dac_bytes(self):
        self.assertEqual(_decoded(_fm(0, 0x22, 0x0B), _fm(0, 0x2A, 0x80), _fm(0, 0x2B, 0x80)),
                         [("YM", "LFO $0B"), ("YM", "DAC on")])


class Psg(unittest.TestCase):
    def test_a_tone_latch_and_its_data_byte_as_one(self):
        # PSG2 tone: latch $A6 (low 6), data $34: (0x34 << 4) | 6 = 838
        self.assertEqual(_decoded(_psg(0xA6), _psg(0x34)), [("PSG2", "tone 838")])

    def test_attenuation_noise_and_a_lone_latch(self):
        self.assertEqual(_decoded(_psg(0xBF), _psg(0xE4), _psg(0x80), _psg(0xD5)),
                         [("PSG2", "att F"), ("NOISE", "form E4"), ("PSG1", "tone lo 0"), ("PSG3", "att 5")])



class ToneRegister(unittest.TestCase):
    def _register(self, op: VgmOp, port: int, reg: int, value: int) -> int | None:
        return tone_register(VgmWrite(0, op, port, reg, value))

    def test_each_parts_frequency_registers_apart(self):
        registers = {self._register(*_fm(port, reg, 0)) for port in (0, 1) for reg in (0xA0, 0xA4, 0xAC)}
        self.assertEqual(len(registers - {None}), 6)

    def test_none_for_any_other_fm_register(self):
        self.assertEqual({self._register(*_fm(0, reg, 0)) for reg in (0x28, 0xB0, 0x40)}, {None})

    def test_a_psg_tone_latch_only(self):
        tones = [self._register(*_psg(b)) for b in (0x86, 0xA6, 0xC6)]
        self.assertEqual(len(set(tones) - {None}), 3)
        self.assertEqual({self._register(*_psg(b)) for b in (0x34, 0xBF, 0xE4)}, {None})   # data, att, noise


if __name__ == "__main__":
    unittest.main()
