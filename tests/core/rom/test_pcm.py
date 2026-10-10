"""DAC samples as a Z80 player stores them (core/rom/pcm.py): the table, an entry's format.

    python -m pytest tests/core/rom/test_pcm.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.chips import MD_PSG_CLOCK
from core.rom import RomError
from core.rom.pcm import Dpcm, Pcm, PcmEntry, PcmTable, SampleFormat, one_format

_BANK = 0x8000               # the Z80's bank window: the table at its start, the samples after it
_ENTRY = PcmEntry(6, size_at=2, pitch_at=4)
_RAW = 0x01                  # the entry's flags byte (+5): raw PCM, else DPCM
_DPCM = Dpcm(bytes(range(16)), 100, 10)
_PCM = Pcm(50, 10)
_NAMES = {0x81: "dKick", 0x82: "dSnare"}


def _entry(start: int, size: int, pitch: int, flags: int) -> bytes:
    return start.to_bytes(2, "little") + size.to_bytes(2, "little") + bytes([pitch, flags])


def _bank_memory() -> bytes:
    """$81: 2 DPCM bytes at pitch 1; $82: 3 raw bytes at pitch 3."""
    samples = _BANK + 2 * _ENTRY.size
    table = _entry(samples, 2, 1, 0) + _entry(samples + 2, 3, 3, _RAW)
    return bytes(_BANK) + table + bytes([0x12, 0x00]) + bytes([0x80, 0xFF, 0x00])


def _by_flags(entry: bytes) -> SampleFormat:
    return _PCM if entry[5] & _RAW else _DPCM


class Table(unittest.TestCase):
    def test_a_table_in_a_bank_each_entry_its_own_format(self):
        table = PcmTable(_bank_memory(), _BANK, _ENTRY, _by_flags, _NAMES)
        kick, snare = table.sample(0x81), table.sample(0x82)

        # DPCM: deltas 1 2 0 0 on $80; raw: each byte signed
        self.assertEqual(kick.pcm, bytes([0x01, 0x03, 0x03, 0x03]))
        self.assertEqual(snare.pcm, bytes([0x00, 0x7F, 0x80]))
        self.assertAlmostEqual(kick.rate, MD_PSG_CLOCK / 100)
        self.assertAlmostEqual(snare.rate, MD_PSG_CLOCK / 70)            # 50 + 2 pitch steps of 10

    def test_a_pitched_copy_plays_at_its_own_pitch_in_its_sample_format(self):
        table = PcmTable(_bank_memory(), _BANK, _ENTRY, _by_flags, _NAMES)
        copy = table.pitched_copy(table.sample(0x82), 0x81, 1)
        self.assertAlmostEqual(copy.rate, MD_PSG_CLOCK / 50)

    def test_an_empty_entry_is_an_error(self):
        memory = bytes(_BANK) + _entry(_BANK, 0, 1, 0)
        with self.assertRaisesRegex(RomError, r"\$81"):
            PcmTable(memory, _BANK, _ENTRY, one_format(_DPCM), _NAMES).sample(0x81)


if __name__ == "__main__":
    unittest.main()
