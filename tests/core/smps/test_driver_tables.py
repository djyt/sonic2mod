"""A driver's PSG table (core/smps/driver_tables.py PsgTable): the pitch of a note index is read
from the driver's own table, not assumed to be Sonic 1's (C3 up, 69 notes).

    python -m pytest tests/core/smps/test_driver_tables.py -q
"""

from __future__ import annotations

import unittest

from core.drivers.reference import SONIC1_RULES
from core.smps import PsgTable, chip_pitch
from core.source import read_song
from tests.roms import SPACE_HARRIER_2_ROM, needs_space_harrier_2

_A2, _C3 = 33, 36           # SMPS semitones (C0 = 0)


class Sonic1(unittest.TestCase):
    def test_its_table_starts_at_c3_and_runs_69_notes(self):
        table = SONIC1_RULES.psg_table
        self.assertEqual((table.pitch, table.notes), (_C3, 69))
        self.assertEqual(chip_pitch(0, 0, True, table), _C3)
        self.assertEqual(chip_pitch(68, 0, True, table), _C3 + 68)

    def test_an_index_past_the_table_plays_the_word_there(self):
        # Credits' PSG1 transposed below the table: index 126 reads 922, a B2 just under C3
        table = SONIC1_RULES.psg_table
        self.assertEqual(table.divider(126), 922)
        self.assertEqual(table.semitone(126), _C3 - 1)


class OtherTables(unittest.TestCase):
    def test_a_table_starting_elsewhere_keeps_its_own_pitch(self):
        # A2 up, an octave and a bit (Space Harrier II's first dividers)
        dividers = (1016, 959, 905, 854, 806, 761, 718, 678, 640, 604, 570, 538, 508, 479)
        table = PsgTable.of(dividers, dividers)
        self.assertEqual((table.pitch, table.notes), (_A2, len(dividers)))
        self.assertEqual(table.semitone(3), _C3)

    def test_a_table_that_is_not_chromatic_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "not a chromatic table"):
            PsgTable.of((854, 806, 700, 678), (854, 806, 700, 678), "test")

    def test_no_table_plays_no_psg_notes(self):
        self.assertEqual(PsgTable.of((), ()).semitone(0), _C3)       # the SMPS convention, nothing read


@needs_space_harrier_2
class SpaceHarrier2(unittest.TestCase):
    def test_its_table_starts_at_a2_and_runs_72_notes(self):
        table = read_song(SPACE_HARRIER_2_ROM, rom_song=0x81).rules.psg_table
        self.assertEqual((table.pitch, table.notes), (_A2, 72))
        self.assertEqual(table.semitone(0), _A2)           # was read as C3: 3 semitones sharp
