"""How far into each sample a written MOD is heard (core/mod/reach.py), and the one-shots cut there
(ModFile.trim_tails), on hand-built MODs.  Speed 6 at 125 BPM: a tick is 20 ms; C-2 (period 428)
plays 8287 bytes a second, 170.7 a tick with the 3 % margin.

    python -m pytest tests/core/mod/test_reach.py -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.mod import (
    PAL_AMIGA_CLOCK,
    PERIOD_TABLE,
    ModFile,
    ModNote,
    UnmodelledEffect,
    heard_bytes,
    read_mod,
)
from core.mod.reach import SPEED_MARGIN

TICK_BYTES = PAL_AMIGA_CLOCK / PERIOD_TABLE[ModNote.C2.value] * 0.02 * SPEED_MARGIN


def _mod(size: int = 8000, loop: tuple[int, int] | None = None) -> ModFile:
    mod = ModFile(4)
    s = mod.samples[0]
    s.data, s.length = bytes(size), size // 2
    s.set_volume(64)
    if loop:
        s.repeat, s.repeat_length = loop[0] // 2, loop[1] // 2
    return mod


def _put(mod: ModFile, row: int, col: int, note: bool = False, eff: tuple[int, int] = (0, 0)) -> None:
    mod.set_cursor(0, col, row)
    if note:
        mod.set_note(ModNote.C2, 1)
    if eff != (0, 0):
        mod.set_effect(*eff)


def _heard(mod: ModFile) -> dict[int, int | None]:
    return heard_bytes(read_mod(bytes(mod.get_bytes())), PAL_AMIGA_CLOCK)


class Reach(unittest.TestCase):
    def test_a_note_is_heard_until_its_cut(self):
        mod = _mod()
        _put(mod, 0, 0, note=True)
        _put(mod, 1, 0, eff=(0xC, 0))                       # C00 a row later: 6 ticks
        self.assertEqual(_heard(mod), {1: math.ceil(6 * TICK_BYTES)})

    def test_a_note_cut_inside_its_row(self):
        mod = _mod()
        _put(mod, 0, 0, note=True, eff=(0xE, 0xC2))         # EC2: 2 ticks
        self.assertEqual(_heard(mod), {1: math.ceil(2 * TICK_BYTES)})

    def test_a_slide_to_silence_ends_what_is_heard(self):
        mod = _mod()
        _put(mod, 0, 0, note=True, eff=(0xA, 0x0F))         # 64 49 34 19 4 0: silent from tick 5
        _put(mod, 1, 0, eff=(0xC, 0))
        self.assertEqual(_heard(mod), {1: math.ceil(5 * TICK_BYTES)})

    def test_a_note_about_to_be_replaced_runs_at_the_faster_period(self):
        mod = _mod()
        _put(mod, 0, 0, note=True)
        mod.set_cursor(0, 0, 1)
        mod.set_note(ModNote.C3, 1)                         # an octave up, 2 ticks late
        mod.set_effect(0xE, 0xD2)
        _put(mod, 2, 0, eff=(0xC, 0))
        # 6 ticks at C-2, then 2 at C-3's rate (twice C-2's); the C-3 note itself is 4 ticks in
        self.assertEqual(_heard(mod), {1: math.ceil((6 + 2 * 2) * TICK_BYTES)})

    def test_an_offset_note_starts_into_its_sample(self):
        mod = _mod()
        _put(mod, 0, 0, note=True, eff=(0x9, 0x10))         # 4096 bytes in
        _put(mod, 1, 0, eff=(0xC, 0))
        self.assertEqual(_heard(mod), {1: math.ceil(0x1000 + 6 * TICK_BYTES)})

    def test_the_last_note_of_a_song_that_stops_may_ring_to_the_end(self):
        mod = _mod()
        _put(mod, 60, 0, note=True)
        self.assertEqual(_heard(mod), {1: None})

    def test_a_note_ringing_across_the_loop_is_followed_through_it(self):
        mod = _mod()
        _put(mod, 1, 0, eff=(0xC, 0))
        _put(mod, 62, 0, note=True)
        _put(mod, 63, 1, eff=(0xB, 0))                      # back to the start: rows 62, 63, 0 heard
        self.assertEqual(_heard(mod), {1: math.ceil(18 * TICK_BYTES)})

    def test_a_looped_sample_is_left_out(self):
        mod = _mod(loop=(1000, 2000))
        _put(mod, 0, 0, note=True)
        _put(mod, 1, 0, eff=(0xC, 0))
        self.assertEqual(_heard(mod), {})

    def test_an_effect_it_does_not_model_is_refused(self):
        mod = _mod()
        _put(mod, 0, 0, note=True, eff=(0x0, 0x47))         # arpeggio
        with self.assertRaises(UnmodelledEffect):
            _heard(mod)

    def test_the_loop_target_is_reported(self):
        mod = _mod()
        _put(mod, 63, 1, eff=(0xB, 0))
        rows, loop_at = read_mod(bytes(mod.get_bytes())).play_order()
        self.assertEqual((len(rows), loop_at), (64, 0))
        self.assertIsNone(read_mod(bytes(_mod().get_bytes())).play_order()[1])


class TrimTails(unittest.TestCase):
    def test_one_shots_are_cut_to_whole_words(self):
        mod = _mod()
        self.assertEqual(mod.trim_tails({1: 1001}), {1: (8000, 1002)})
        self.assertEqual((mod.samples[0].length, len(mod.samples[0].data)), (501, 1002))

    def test_files_loops_and_whole_notes_stay(self):
        mod = _mod()
        mod.samples[0].file = "kick.raw"                    # the same drum in other MODs
        looped = _mod(loop=(1000, 2000))
        self.assertEqual(mod.trim_tails({1: 100}), {})
        self.assertEqual(looped.trim_tails({1: 100}), {})
        self.assertEqual(_mod().trim_tails({1: None}), {})


if __name__ == "__main__":
    unittest.main()
