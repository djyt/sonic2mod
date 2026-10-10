"""Music and SFX headers read from a ROM (core/rom/header.py).

    python -m pytest tests/core/rom/test_header.py -q
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.sonic1 import SONIC1
from core.rom import RomImage
from core.rom.header import read_music_header, read_sfx_header
from core.rom.variant import EntryLayout
from core.smps import (
    NO_TEMPO_HOLDS,
    ChannelType,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


_SONG = 0x200          # where the hand-built songs start


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


def _memory(song: bytes) -> Relative68kMemory:
    """The hand-built ROM as the 68k drivers read it."""
    return Relative68kMemory(_rom(song))


def _music(tracks: list[bytes], tempo: tuple[int, int] = (1, 3), voices: bytes = b"") -> bytes:
    """A music header with a DAC track and FM tracks, the code after it, then the voices."""
    fixed = 6 + 4 * len(tracks)
    offsets, at = [], fixed
    for t in tracks:
        offsets.append(at)
        at += len(t)
    head = (at if voices else 0).to_bytes(2, "big") + bytes([len(tracks), 0, *tempo])
    head += b"".join(o.to_bytes(2, "big") + bytes([0xF4, 0x08]) for o in offsets)
    return head + b"".join(tracks) + voices


class Headers(unittest.TestCase):
    def test_music_tracks_are_relative_to_the_header(self):
        head = read_music_header(_memory(_music([b"\xF2", b"\xF2"], tempo=(2, 5))), _SONG, SONIC1.header)
        self.assertEqual([c.channel_type for c in head.header.channels], ["DAC", "FM"])
        self.assertEqual((head.header.tempo_divider, head.header.tempo_modifier), (2, 5))
        self.assertEqual(head.header.channels[1].pitch_offset, -12)
        self.assertEqual(head.tracks, {_SONG + 14: ChannelType.DAC, _SONG + 15: ChannelType.FM})

    def test_a_layout_without_tempo_reads_its_own_entries(self):
        # voices.w fm.b psg.b, FM ptr.w volume.b, PSG ptr.w volume.b envelope.b: Streets of Rage's shape
        layout = dataclasses.replace(SONIC1.header, tempo=False, fm_entry=EntryLayout(3, volume=2),
                                     psg_entry=EntryLayout(4, volume=2, envelope=3))
        song = bytes([0, 0, 2, 1]) + bytes([0, 11, 4]) + bytes([0, 12, 5]) + bytes([0, 13, 7, 3]) + bytes([0xF2] * 3)
        head = read_music_header(_memory(song), _SONG, layout)
        _dac, fm, psg = head.header.channels
        self.assertEqual((head.header.tempo_divider, head.header.tempo_modifier), (1, NO_TEMPO_HOLDS))
        self.assertEqual((fm.volume, fm.pitch_offset, psg.volume, psg.psg_voice_label), (5, 0, 7, "fTone_03"))
        self.assertEqual(head.tracks, {_SONG + 11: ChannelType.DAC, _SONG + 12: ChannelType.FM, _SONG + 13: ChannelType.PSG})

    def test_sfx_channels_carry_their_hardware_channel(self):
        sfx = bytes([0, 0, 1, 1, 0x80, 0xC0, 0, 10, 0xF4, 0x02, 0xF2])
        head = read_sfx_header(_memory(sfx), _SONG, SONIC1.header)
        ch = head.header.channels[0]
        self.assertTrue(head.header.is_sfx)
        self.assertEqual((ch.channel_type, ch.hw_channel, ch.pitch_offset, ch.volume), ("PSG", 0xC0, -12, 2))
        self.assertIsNone(head.voices)


if __name__ == "__main__":
    unittest.main()
