"""Track decoding (core/rom/tracks.py) on hand-built bytes: loops, flags and their operands, calls.

    python -m pytest tests/core/rom/test_tracks.py -q
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers import (
    read_rom_song,
)
from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.sonic1 import SONIC1
from core.rom import RomError, RomImage
from core.rom.tracks import decode_tracks
from core.smps import (
    ChannelType,
    CoordFlag,
    OpKind,
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


def _pointer(at: int, target: int) -> bytes:
    """A jump / call operand at song offset `at`: target = its address + 1 + signed word."""
    return (target - at - 1).to_bytes(2, "big", signed=True)


def _index():
    """The hand-built ROMs hold one song at _SONG: an index of it alone."""
    from core.rom import SoundIndex
    return SoundIndex(music={0x81: _SONG}, sfx={})


class Tracks(unittest.TestCase):
    def test_a_backward_jump_is_the_loop(self):
        # DAC: rest $10, stop.  FM1: note $0C, jump back to its own note
        dac = bytes([0x80, 0x10, 0xF2])
        fm_at = 6 + 8 + len(dac)
        fm = bytes([0xA0, 0x0C, 0xF6]) + _pointer(fm_at + 3, fm_at)
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index(), variant=SONIC1)
        fm1 = song.channels[1]
        self.assertTrue(fm1.has_jump)
        self.assertEqual((fm1.loop_tick, fm1.loop_event_index), (0, None))   # its own start: by tick, as the parser
        self.assertEqual([(e.note.note_value, e.note.duration) for e in fm1.events], [(0xA0, 0x0C)])

    def test_flags_take_their_operands_and_signed_ones_are_signed(self):
        dac = bytes([0xF0, 1, 2, 3, 4, 0xE9, 0xF4, 0xE6, 0x02, 0x80, 0x01, 0xF2])
        code = decode_tracks(_memory(_music([dac])), {_SONG + 10: ChannelType.DAC}, SONIC1).code
        effects = [op.effect for op in code.ops if op.kind is OpKind.EFFECT]
        self.assertEqual([(e.flag, list(e.values)) for e in effects],
                         [(CoordFlag.MOD_SET, [1, 2, 3, 4]), (CoordFlag.CHANGE_TRANSPOSITION, [-12]),
                          (CoordFlag.ALTER_VOL, [2])])

    def test_a_label_between_a_note_and_its_duration_leaves_the_duration_the_notes(self):
        # note $A0, then a loop target AT the duration byte $06: the note still lasts $06
        dac = bytes([0xF2])
        fm_at = 6 + 8 + len(dac)
        fm = bytes([0xA0, 0x06, 0xF7, 0x00, 0x02]) + _pointer(fm_at + 5, fm_at + 1) + bytes([0xF2])
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index(), variant=SONIC1)
        notes = [(e.note.note_value, e.note.duration, e.note.is_retrigger) for e in song.channels[1].events]
        # the replay re-reads $06 alone: a standalone duration re-keys the note
        self.assertEqual(notes, [(0xA0, 6, False), (0xA0, 6, True)])

    def test_a_call_is_inlined_and_returns(self):
        dac = bytes([0xF2])
        fm_at = 6 + 8 + len(dac)
        sub = fm_at + 5
        fm = bytes([0xF8]) + _pointer(fm_at + 1, sub) + bytes([0xF2, 0x00]) + bytes([0xB0, 0x04, 0xE3])
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index(), variant=SONIC1)
        self.assertEqual([e.note.note_value for e in song.channels[1].events], [0xB0])

    def test_an_unknown_flag_names_its_address(self):
        with self.assertRaisesRegex(RomError, r"\$20A: \$FB"):
            decode_tracks(_memory(_music([bytes([0xFB])])), {_SONG + 10: ChannelType.FM}, SONIC1)

    def test_code_two_kinds_share_needs_one_flag_table(self):
        psg = {**SONIC1.flags[ChannelType.PSG]}
        variant = dataclasses.replace(SONIC1, flags={**SONIC1.flags, ChannelType.PSG: psg})
        memory = _memory(_music([bytes([0x80, 0x01, 0xF2])]))
        with self.assertRaisesRegex(RomError, r"\$20C: code shared by PSG and FM tracks"):
            decode_tracks(memory, {_SONG + 10: ChannelType.FM, _SONG + 12: ChannelType.PSG}, variant)
        decode_tracks(memory, {_SONG + 10: ChannelType.FM, _SONG + 12: ChannelType.PSG}, SONIC1)

    def test_smpsFade_and_smpsStopSpecial_end_the_track(self):
        for flag in (0xE4, 0xEE):
            code = decode_tracks(_memory(_music([bytes([0x80, 0x01, flag, 0xA0, 0x01])])), {_SONG + 10: ChannelType.FM}, SONIC1).code
            self.assertIs(code.ops[-1].kind, OpKind.STOP)


if __name__ == "__main__":
    unittest.main()
