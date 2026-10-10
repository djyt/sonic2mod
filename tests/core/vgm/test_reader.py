"""The VGM reader (core/vgm/reader.py) on hand-built logs.

    python -m pytest tests/core/vgm/test_reader.py -q
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.vgm import (
    VgmError,
    VgmOp,
    decode_vgm,
)
from tests.vgm_build import HEADER_BYTES as _HEADER_BYTES
from tests.vgm_build import fm as _fm
from tests.vgm_build import psg as _psg
from tests.vgm_build import vgm as _vgm
from tests.vgm_build import wait as _wait


class Reader(unittest.TestCase):
    def test_writes_are_timed_by_the_waits_before_them(self):
        log = decode_vgm(_vgm(_fm(0, 0x28, 0xF0) + b"\x62" + _psg(0x9F) + b"\x70" + _fm(1, 0xA4, 0x22)))
        self.assertEqual([(w.sample, w.op, w.port, w.reg, w.value) for w in log.writes],
                         [(0, VgmOp.FM, 0, 0x28, 0xF0), (735, VgmOp.PSG, 0, 0, 0x9F), (736, VgmOp.FM, 1, 0xA4, 0x22)])
        self.assertEqual(log.end_sample, 736)
        self.assertEqual(log.header.fm_clock, 7_670_453)

    def test_dac_bytes_come_from_the_bank_and_seek_moves_the_pointer(self):
        bank = bytes((0x10, 0x20, 0x30, 0x40))
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        seek = b"\xE0" + struct.pack("<I", 2)
        log = decode_vgm(_vgm(block + b"\x82\x83" + seek + b"\x81"))
        dac = [(w.sample, w.value) for w in log.writes if w.op is VgmOp.FM]
        self.assertEqual(dac, [(0, 0x10), (2, 0x20), (5, 0x30)])
        self.assertEqual([w.value for w in log.writes if w.op is VgmOp.PCM_SEEK], [2])
        self.assertEqual(log.pcm, bank)

    def test_the_loop_offset_becomes_a_sample(self):
        commands = _wait(100) + _psg(0x9F) + _wait(50)
        log = decode_vgm(_vgm(commands, loop_at=3))
        self.assertEqual(log.loop_sample, 100)

    def test_other_chips_are_skipped_and_unknown_commands_refused(self):
        log = decode_vgm(_vgm(bytes((0x51, 0x01, 0x02, 0xA0, 0x01, 0x02)) + _psg(0x9F)))
        self.assertEqual(len(log.writes), 1)
        with self.assertRaises(VgmError):
            decode_vgm(_vgm(b"\x6F"))
        with self.assertRaises(VgmError):
            decode_vgm(b"RIFF" + bytes(_HEADER_BYTES))


if __name__ == "__main__":
    unittest.main()
