"""A driver's PSG table (core/smps/driver_tables.py PsgTable): the pitch of a note index is read
from the driver's own table, not assumed to be Sonic 1's (C3 up, 69 notes).

    python -m pytest tests/core/smps/test_driver_tables.py -q
"""

from __future__ import annotations

import math
import unittest

from core.audio import semitone_to_hz
from core.chips import MD_PSG_CLOCK, freq_word_hz, psg_divider_for_hz, psg_frequency_hz
from core.drivers.reference import SONIC1_RULES
from core.smps import PitchRules, PsgTable, chip_pitch, fm_render_word, fm_table_index, played_song, psg_render_divider
from core.source import read_song
from tests.roms import SPACE_HARRIER_2_ROM, STREETS_OF_RAGE_2_ROM, needs_space_harrier_2, needs_streets_of_rage_2

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

    def test_a_sample_renders_at_the_tables_divider_from_its_own_pitch(self):
        # Streets of Rage 2's PSG was rendered three entries down, read as a table from C3
        dividers = (1016, 959, 905, 854, 806, 761, 718, 678, 640, 604, 570, 538, 508, 479)
        table = PsgTable.of(dividers, dividers)
        self.assertEqual(table.render_divider(_C3), 854)
        self.assertAlmostEqual(table.divider_cents(_C3), 1200 * math.log2(psg_frequency_hz(854, MD_PSG_CLOCK)
                                                                         / semitone_to_hz(_C3)))

    def test_off_the_run_a_sample_renders_at_equal_temperament(self):
        table = PsgTable.of(SONIC1_RULES.psg_frequencies, SONIC1_RULES.psg_read)
        self.assertEqual(table.render_divider(_A2), psg_divider_for_hz(semitone_to_hz(_A2)))
        self.assertLess(abs(table.divider_cents(_A2)), 2)

    def test_an_fm_sample_renders_at_the_tables_word_else_equal_temperament(self):
        frequencies = SONIC1_RULES.fm_frequencies
        self.assertEqual(fm_render_word(_C3, frequencies), frequencies[fm_table_index(_C3)])
        self.assertLess(abs(1200 * math.log2(freq_word_hz(fm_render_word(-12, frequencies)) / semitone_to_hz(-12))), 3)

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


class Nearest(unittest.TestCase):
    _C4, _B7, _FS3 = 48, 95, 42

    def test_a_small_detune_keeps_its_note(self):
        # C4's 427 + 2: 8 c
        table = SONIC1_RULES.psg_table
        self.assertEqual(table.nearest(table.render_divider(self._C4) + 2), (self._C4, 2))

    def test_a_detune_past_half_a_semitone_moves_the_note(self):
        # B7's 29 + 2 = 31: A#7's
        self.assertEqual(SONIC1_RULES.psg_table.nearest(31), (self._B7 - 1, 0))

    def test_a_ramp_lands_on_the_note_its_divider_plays(self):
        # Streets of Rage's $8F PSG1: B7 + 567 = 596, an F#3 (604) - 8
        self.assertEqual(SONIC1_RULES.psg_table.nearest(596), (self._FS3, -8))


class Divider(unittest.TestCase):
    def test_a_word_past_10_bits_wraps(self):
        # Streets of Rage's $8F PSG1: 29 + 997 is 2, its ramp from the top again
        self.assertEqual(PitchRules().divider(29 + 997), 2)

    def test_a_clamping_driver_holds_the_ends(self):
        # Streets of Rage 2's
        rules = PitchRules(divider_clamp=True)
        self.assertEqual((rules.divider(29 + 997), rules.divider(-3)), (1023, 1))


@needs_streets_of_rage_2
class StreetsOfRage2(unittest.TestCase):
    def test_a_sample_renders_at_the_divider_the_driver_writes(self):
        # Go Straight's PSG1 at detune -2: the driver shifts its word (-16) by A#4's octave (3) as it
        # plays; the render adds what it wrote, 240 - 2
        song = read_song(STREETS_OF_RAGE_2_ROM, rom_song=0x00)
        first = next(n for n in played_song(song).channels["PSG1"] if not n.rest)
        self.assertEqual((first.note, first.pitch), (240, 238))
        self.assertEqual(psg_render_divider(song.rules, _A2 + 25, first.pitch - first.note), 238)
