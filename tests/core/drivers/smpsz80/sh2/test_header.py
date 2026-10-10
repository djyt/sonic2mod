"""Space Harrier II's track lists (core/drivers/smpsz80/sh2/header.py) on a hand-built bank.

    python -m pytest tests/core/drivers/smpsz80/sh2/test_header.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers.smpsz80.sh2.header import is_track_list, read_track_list
from core.drivers.smpsz80.sh2.memory import DriverTables, Sh2Memory
from core.rom import RomError, RomImage
from core.smps import NO_TEMPO_HOLDS, ChannelType

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_BANK = 0x8000
_TEMPOS, _INDEX, _LIST, _VOICES = 0x00, 0x02, 0x10, 0x80     # bank offsets
_CODE = 0xC0                   # each track's code: a stop
_CHANNELS = (0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0xC0)
_DRUMS = 2                     # the drum track's slot (FM3); the PSG half's is the last


def _z80(at: int) -> bytes:
    return (0x8000 + at).to_bytes(2, "little")


def _record(channel: int, code: int, flags: int = 0x80, divider: int = 2) -> bytes:
    """flags, channel, divider, pointer, transposition -8, envelope 1, voice $90, volume $18"""
    return bytes([flags, channel, divider]) + _z80(code) + bytes([0xF8, 1, 0x90, 0x18])


def _memory(records: list[bytes], tempo: int = 0) -> Sh2Memory:
    bank = bytearray(0x100)
    bank[_TEMPOS] = tempo
    bank[_INDEX:_INDEX + 2] = _z80(_LIST)
    track_list = bytes([len(records)]) + b"".join(records)
    bank[_LIST:_LIST + len(track_list)] = track_list
    bank[_CODE:_CODE + 7] = bytes([0xF2] * 7)
    rom = bytearray(_HEADER.ljust(2 * _BANK, b"\0"))
    rom[_BANK:_BANK + len(bank)] = bank
    return Sh2Memory(RomImage(bytes(rom)), DriverTables(_BANK, _BANK + _TEMPOS, _BANK + _INDEX, 1, _BANK + _VOICES, _BANK))


def _song(changes: dict[int, bytes] | None = None) -> list[bytes]:
    """Seven records, each track at its own stop, the PSG half at the drum track's; `changes`: a
    record by slot (0-based) in their place."""
    records = [_record(channel, _CODE + slot) for slot, channel in enumerate(_CHANNELS)]
    records[-1] = _record(0xC0, _CODE + _DRUMS)
    for slot, record in (changes or {}).items():
        records[slot] = record
    return records


class TrackLists(unittest.TestCase):
    def test_the_drum_track_and_fm1_to_fm6(self):
        head = read_track_list(_memory(_song()), _BANK + _LIST)
        channels = head.header.channels
        self.assertEqual([(c.channel_type, c.chip_channel) for c in channels],
                         [(ChannelType.FM, "FM1"), (ChannelType.FM, "FM2"), (ChannelType.DAC, "FM3"),
                          (ChannelType.FM, "FM4"), (ChannelType.FM, "FM5"), (ChannelType.FM, "FM6")])
        self.assertEqual((channels[0].pitch_offset, channels[0].volume), (-8, 0x18))
        self.assertEqual((head.header.fm_count, head.header.psg_count, head.header.tempo_divider), (6, 0, 2))
        self.assertEqual(head.voices, _BANK + _VOICES)
        self.assertEqual(head.tracks[_BANK + _CODE + _DRUMS], ChannelType.DAC)

    def test_the_tempo_comes_from_the_table_by_song(self):
        self.assertEqual(read_track_list(_memory(_song()), _BANK + _LIST).header.tempo_modifier, NO_TEMPO_HOLDS)
        self.assertEqual(read_track_list(_memory(_song(), tempo=3), _BANK + _LIST).header.tempo_modifier, 3)

    def test_a_track_that_does_not_play_is_left_out(self):
        head = read_track_list(_memory(_song({4: _record(0x05, _CODE + 4, flags=0)})), _BANK + _LIST)
        self.assertEqual([c.chip_channel for c in head.header.channels], ["FM1", "FM2", "FM3", "FM4", "FM6"])

    def test_the_psg_half_must_read_the_drum_track(self):
        with self.assertRaisesRegex(RomError, "not the drum track's PSG half"):
            read_track_list(_memory(_song({6: _record(0xC0, _CODE + 6)})), _BANK + _LIST)

    def test_the_slot_decides_how_a_track_plays(self):
        # FM6's channel in FM3's slot: the driver would play it as drums
        with self.assertRaisesRegex(RomError, "slot 3 on FM6"):
            read_track_list(_memory(_song({2: _record(0x06, _CODE + 2)})), _BANK + _LIST)

    def test_one_divider_per_song(self):
        with self.assertRaisesRegex(RomError, "dividers"):
            read_track_list(_memory(_song({1: _record(0x01, _CODE + 1, divider=3)})), _BANK + _LIST)

    def test_a_channel_byte_that_names_no_channel_is_no_track_list(self):
        self.assertTrue(is_track_list(_memory(_song()), _BANK + _LIST))
        self.assertFalse(is_track_list(_memory(_song({1: _record(0x03, _CODE + 1)})), _BANK + _LIST))


if __name__ == "__main__":
    unittest.main()
