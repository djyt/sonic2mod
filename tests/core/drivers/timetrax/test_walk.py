"""Time Trax's song walk and grammar (core/drivers/timetrax/walk.py, grammar.py) on hand-built tracks:
fixed durations, loop ends, calls, the sync, and each byte's reading.

    python -m pytest tests/core/drivers/timetrax/test_walk.py -q
"""

from __future__ import annotations

import unittest

from core.drivers.timetrax import TIMETRAX
from core.drivers.timetrax.grammar import instruction
from core.drivers.timetrax.memory import DriverTables, TimeTraxMemory
from core.drivers.timetrax.walk import walk_song
from core.rom import RomError, RomImage
from core.smps import REST, ChannelType, Detune, Legato, OpKind, Pan, SetVoice

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_BANK = 0x8000
_PANS = (0x80, 0xC0, 0x40)


def _memory(*tracks: bytes) -> tuple[TimeTraxMemory, list[int]]:
    """Each track's bytes in the bank, $100 apart from Z80 $8100: the memory and their ROM addresses."""
    rom = bytearray(_HEADER.ljust(2 * _BANK, b"\0"))
    starts = []
    for i, code in enumerate(tracks):
        at = _BANK + 0x100 * (i + 1)
        rom[at:at + len(code)] = code
        starts.append(at)
    tables = DriverTables(_BANK, 0, 0, 0, tuple(range(96)), _PANS)
    return TimeTraxMemory(RomImage(bytes(rom)), tables), starts


def _jump(track: int) -> bytes:
    """$81 to the start of track `track` (0: Z80 $8100)."""
    return bytes([0x81]) + (0x8100 + 0x100 * track).to_bytes(2, "big")


class Walk(unittest.TestCase):
    def test_a_fixed_duration_drops_the_byte_after_each_note(self):
        memory, (start,) = _memory(bytes([0x10, 0x06, 0x86, 0x0C, 0x11, 0x12, 0x86, 0x00, 0x13, 0x03, 0x80]))
        reading = walk_song(memory, {start: 0})
        self.assertEqual(reading.fixed, {start: 0, start + 4: 12, start + 5: 12, start + 8: 0})

    def test_a_loop_end_closes_the_loop_open_on_its_track(self):
        # $82 2 [ $82 3 [ note ] $83 ] $83
        memory, (start,) = _memory(bytes([0x82, 0x02, 0x82, 0x03, 0x10, 0x01, 0x83, 0x83, 0x80]))
        reading = walk_song(memory, {start: 0})
        self.assertEqual(reading.loop_ends, {start + 6: (start + 4, 3), start + 7: (start + 2, 2)})

    def test_code_reached_under_two_fixed_durations_is_refused(self):
        # A call from free time, then from fixed time: the called note reads two ways
        memory, (start, _) = _memory(bytes([0x84, 0x82, 0x00, 0x86, 0x05, 0x84, 0x82, 0x00, 0x80]),
                                     bytes([0x10, 0x01, 0x85]))
        with self.assertRaises(RomError):
            walk_song(memory, {start: 0})

    def test_a_wait_lasts_to_its_first_read_after_the_signal(self):
        # FM1 signals FM3 ($8D 3) at frame 7; FM3 waits from frame 2, reading $8E at 2 4 6 8: it goes
        # on at 8
        memory, (sender, waiter) = _memory(bytes([0x10, 0x07, 0x8D, 0x03, 0x10, 0x10, 0x80]),
                                           bytes([0x10, 0x02, 0x8E, 0x10, 0x01, 0x80]))
        reading = walk_song(memory, {sender: 0, waiter: 2})
        self.assertEqual(reading.waits, {waiter + 2: 6})

    def test_a_wait_no_track_signals_is_refused(self):
        memory, (start,) = _memory(bytes([0x10, 0x02, 0x8E, 0x80]))
        with self.assertRaises(RomError):
            walk_song(memory, {start: 0})

    def test_a_command_no_song_plays_is_refused(self):
        memory, (start,) = _memory(bytes([0xA6, 0x01, 0x80]))
        with self.assertRaises(RomError):
            walk_song(memory, {start: 0})

    def test_a_track_ends_at_its_loops_jump(self):
        memory, (start,) = _memory(bytes([0x10, 0x04, *_jump(0)]))
        reading = walk_song(memory, {start: 0})
        self.assertEqual(reading.fixed, {start: 0})


class Grammar(unittest.TestCase):
    def _read(self, code: bytes, at: int = 0):
        memory, (start,) = _memory(code)
        memory = memory.with_reading(walk_song(memory, {start: 0}))
        return instruction(memory, start + at, TIMETRAX, ChannelType.FM)

    def test_a_note_plays_the_table_entry_its_byte_names(self):
        one = self._read(bytes([0x10, 0x00, 0x80]))
        self.assertEqual([(op.kind, op.value) for op in one.ops], [(OpKind.NOTE, REST + 0x10), (OpKind.DURATION, 256)])

    def test_a_rest_is_a_rest(self):
        one = self._read(bytes([0x00, 0x05, 0x80]))
        self.assertEqual([(op.kind, op.value) for op in one.ops], [(OpKind.NOTE, REST), (OpKind.DURATION, 5)])

    def test_the_effects_read_as_the_driver_writes_them(self):
        cases = {bytes([0x87, 0xFF]): Pan(0x80), bytes([0x87, 0x01]): Pan(0x40), bytes([0x88, 0x07]): SetVoice(7),
                 bytes([0x8C]): Legato(1), bytes([0x8B]): Legato(0), bytes([0xA8, 0xFE]): Detune(-2),
                 bytes([0xA9]): Detune(0)}
        for code, effect in cases.items():
            with self.subTest(code=code.hex()):
                self.assertEqual(self._read(code + b"\x80").ops[0].effect, effect)

    def test_a_command_not_converted_yet_is_dropped_by_name(self):
        one = self._read(bytes([0x92, 0x04, 0x01, 0x80]))
        self.assertEqual((one.ops, one.length, one.dropped), ((), 3, "glide (not converted yet)"))

    def test_a_loop_end_names_its_body_and_passes(self):
        one = self._read(bytes([0x82, 0x04, 0x10, 0x01, 0x83, 0x80]), at=4)
        self.assertEqual((one.ops[0].kind, one.ops[0].value), (OpKind.LOOP, 4))


if __name__ == "__main__":
    unittest.main()
