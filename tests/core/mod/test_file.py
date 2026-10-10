"""The MOD file (core/mod/file.py) on hand-built MODs: slot compaction, the writer's sample bytes
and name field, narrowing only when the columns beyond are empty.

    python -m pytest tests/core/mod/test_file.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.mod import PERIOD_TABLE, ModFile, ModNote, ModSample, read_mod


def _fill_slot(mod: ModFile, slot: int, size: int, volume: int) -> None:
    s = mod.samples[slot - 1]
    s.data = bytes(range(size))
    s.length = size // 2
    s.set_volume(volume)
    s.repeat, s.repeat_length = 2, size // 4


def _mod() -> ModFile:
    """Slots 1, 3 and 17 hold samples (2 and 4-16 are gaps); notes play 1, 3, 17 and 3 again."""
    mod = ModFile(4)
    _fill_slot(mod, 1, 8, 64)
    _fill_slot(mod, 3, 16, 40)
    _fill_slot(mod, 17, 32, 20)
    for row, (col, slot) in enumerate([(0, 1), (1, 3), (2, 17), (3, 3)]):
        mod.set_cursor(0, col, row)
        mod.set_note(ModNote.C2, slot)
        mod.set_effect(0xC, 0x20 + row)
    return mod


def _sample(data: bytes, volume=64, loop=None) -> ModSample:
    s = ModSample("t")
    s.data = data
    s.length = len(data) // 2
    s.set_volume(volume)
    if loop:
        s.repeat, s.repeat_length = loop[0] // 2, loop[1] // 2
    return s


class CompactTests(unittest.TestCase):
    def test_slots_close_up_and_cells_follow(self):
        mod = _mod()
        mapping = mod.compact_samples()
        self.assertEqual(mapping, {1: 1, 3: 2, 17: 3})
        image = read_mod(bytes(mod.get_bytes()))
        cells = [image.patterns[0][row][col] for row, col in enumerate(range(4))]
        self.assertEqual([c[1] for c in cells], [1, 2, 3, 2])           # instrument numbers renumbered
        self.assertEqual([c[2:] for c in cells], [(0xC, 0x20), (0xC, 0x21), (0xC, 0x22), (0xC, 0x23)])
        self.assertEqual([s.volume for s in image.samples[:3]], [64, 40, 20])  # headers moved with their data
        self.assertEqual([s.length for s in image.samples[:4]], [8, 16, 32, 0])

    def test_nothing_to_close(self):
        mod = ModFile(4)
        _fill_slot(mod, 1, 8, 64)
        _fill_slot(mod, 2, 8, 32)
        self.assertEqual(mod.compact_samples(), {})

    def test_an_empty_slot_a_note_names_is_kept(self):
        mod = ModFile(4)
        _fill_slot(mod, 1, 8, 64)
        mod.set_cursor(0, 0, 0)
        mod.set_note(ModNote.C2, 5)            # an empty slot a note plays (mod_lint's empty_slot) stays visible
        self.assertEqual(mod.compact_samples(), {1: 1, 5: 2})


class ModWriter(unittest.TestCase):
    def test_every_sample_takes_exactly_the_bytes_its_header_declares(self):
        mod = ModFile(4)
        odd = ModSample("odd")
        odd.data = bytes([7] * 5)
        odd.length = 3                                   # 5 bytes, evened to 3 words
        mod.samples[0] = odd
        nxt = ModSample("next")
        nxt.data = bytes([9] * 4)
        nxt.length = 2
        mod.samples[1] = nxt
        data = mod.get_bytes()
        end = len(data)
        self.assertEqual(data[end - 4:], bytes([9] * 4))   # the next sample starts on its own word
        self.assertEqual(data[end - 10:end - 4], bytes([7] * 5) + bytes(1))


    def test_a_one_shot_starts_with_a_silent_word(self):
        # ProTracker replays a one-shot's first word after it ends; a looped sample replays its loop
        mod = ModFile(4)
        one_shot, looped = _sample(bytes([0x82, 0x7E, 5, 6])), _sample(bytes([0x82, 0x7E, 5, 6]), loop=(0, 4))
        mod.samples[0], mod.samples[1] = one_shot, looped
        mod.zero_idle_words()
        self.assertEqual(one_shot.data, bytes([0, 0, 5, 6]))
        self.assertEqual(looped.data, bytes([0x82, 0x7E, 5, 6]))


class Narrowing(unittest.TestCase):
    def test_narrow_only_when_the_columns_beyond_are_empty(self):
        mod = ModFile(8)
        mod.set_channel(1)
        from core.mod import ModNote
        mod.set_note(ModNote.C2, 1)
        self.assertEqual(mod.used_channels(), 2)
        mod.narrow_to(4)
        self.assertEqual(mod.CHANNELS, 4)
        self.assertEqual(mod.MOD_FORMAT, b"M.K.")
        self.assertEqual(mod.note_at(0, 0, 1), PERIOD_TABLE[ModNote.C2.value])
        wide = ModFile(8)
        wide.set_channel(5)
        wide.set_note(ModNote.C2, 1)
        with self.assertRaises(ValueError):
            wide.narrow_to(4)


class WriterTests(unittest.TestCase):
    def test_all_22_characters_are_written(self):
        mod = ModFile(4)
        s = mod.samples[0]
        s.set_name("F5+3+4+P1 F#3 #2 [1-4]")       # 22 characters
        s.data, s.length = bytes(4), 2
        self.assertEqual(read_mod(bytes(mod.get_bytes())).samples[0].name.rstrip("\x00"), "F5+3+4+P1 F#3 #2 [1-4]")


if __name__ == "__main__":
    unittest.main()
