"""Glides on the MOD (core/plan/glides.py): where a sample can follow a curve, a read and its ties.

    python -m pytest tests/core/plan/test_glides.py -q
"""

from __future__ import annotations

import unittest

from core.chips import freq_word
from core.drivers.reference import FM_FREQUENCIES
from core.mod import PERIOD_TABLE
from core.plan import BakedPlan, Read, reaches, with_ties, word_period
from core.smps import REST, SmpsEvent, SmpsNote

_C4, _MIDDLE = 49, 13                   # SMPS C4 (semitone); a MOD note in the middle octave


class Reach(unittest.TestCase):
    def test_its_own_word_plays_at_its_period(self):
        word = FM_FREQUENCIES[_C4 + 1]
        self.assertAlmostEqual(word_period(word, _MIDDLE, _C4, FM_FREQUENCIES), PERIOD_TABLE[_MIDDLE])

    def test_an_octave_up_halves_the_period(self):
        fnum, block = FM_FREQUENCIES[_C4 + 1] & 0x7FF, FM_FREQUENCIES[_C4 + 1] >> 11
        up = freq_word(fnum, block + 1)
        self.assertAlmostEqual(word_period(up, _MIDDLE, _C4, FM_FREQUENCIES), PERIOD_TABLE[_MIDDLE] / 2)

    def test_four_octaves_are_past_the_mod(self):
        fnum, block = FM_FREQUENCIES[_C4 + 1] & 0x7FF, FM_FREQUENCIES[_C4 + 1] >> 11
        self.assertTrue(reaches((freq_word(fnum, block + 1),), _MIDDLE, _C4, FM_FREQUENCIES))
        self.assertFalse(reaches((freq_word(fnum, block - 4),), _MIDDLE, _C4, FM_FREQUENCIES))


class Ties(unittest.TestCase):
    def test_a_read_carries_the_held_rests_after_it(self):
        note, held, rest = (SmpsEvent(SmpsNote(REST + 1, 2)), SmpsEvent(SmpsNote(REST, 1, is_rest=True, is_no_attack=True)),
                            SmpsEvent(SmpsNote(REST, 1, is_rest=True)))
        walked = [Read(note, 2, (1, 2)), Read(held, 1, (3,)), Read(rest, 1, (4,))]
        self.assertEqual([r.words for r in with_ties(walked, 0)], [(1, 2), (3,)])


class Baked(unittest.TestCase):
    def test_a_note_keys_its_shape(self):
        plan = BakedPlan({("FM1", 96): 3}, {})
        self.assertEqual((plan.at("FM1", 96), plan.at("FM1", 97)), (3, 0))


class Levels(unittest.TestCase):
    def test_a_read_holds_its_tls_to_each_change(self):
        read = Read(SmpsEvent(SmpsNote(REST + 1, 3)), 3, (), ((0x4C, 20),), ((1, ((0x4C, 22),)),))
        self.assertEqual(read.levels(), (((0x4C, 20),), ((0x4C, 22),), ((0x4C, 22),)))
