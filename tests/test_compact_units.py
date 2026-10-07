"""Sample slot compaction (ModFile.compact_samples, samples.compact_slots) and the diagnostics that
follow it (Diagnostics.remap_instruments), on a hand-built MOD.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.diagnostics import Diagnostics, InfoKind, WarningKind
from core.mod import ModFile, ModNote, read_mod


def _sample(mod: ModFile, slot: int, size: int, volume: int) -> None:
    s = mod.samples[slot - 1]
    s.data = bytes(range(size))
    s.length = size // 2
    s.set_volume(volume)
    s.repeat, s.repeat_length = 2, size // 4


def _mod() -> ModFile:
    """Slots 1, 3 and 17 hold samples (2 and 4-16 are gaps); notes play 1, 3, 17 and 3 again."""
    mod = ModFile(4)
    _sample(mod, 1, 8, 64)
    _sample(mod, 3, 16, 40)
    _sample(mod, 17, 32, 20)
    for row, (col, slot) in enumerate([(0, 1), (1, 3), (2, 17), (3, 3)]):
        mod.set_cursor(0, col, row)
        mod.set_note(ModNote.C2, slot)
        mod.set_effect(0xC, 0x20 + row)
    return mod


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
        _sample(mod, 1, 8, 64)
        _sample(mod, 2, 8, 32)
        self.assertEqual(mod.compact_samples(), {})

    def test_an_empty_slot_a_note_names_is_kept(self):
        mod = ModFile(4)
        _sample(mod, 1, 8, 64)
        mod.set_cursor(0, 0, 0)
        mod.set_note(ModNote.C2, 5)            # an empty slot a note plays (mod_lint's empty_slot) stays visible
        self.assertEqual(mod.compact_samples(), {1: 1, 5: 2})


class RemapTests(unittest.TestCase):
    def test_slots_in_every_shape_and_counts_left_alone(self):
        diag = Diagnostics()
        diag.warn(WarningKind.SAMPLE_TRUNCATED, channel="FM", extra_ctx="instrument 17", instrument=17)
        diag.info(InfoKind.MERGE_UNUSED, instruments=[3, 17])
        diag.info(InfoKind.MERGE_GROUP, composites=[(17, 4, "x"), (3, 2, "y")], alone=1)
        diag.info(InfoKind.AUTO_SUSTAIN_FM, secs=1.0, shortest=0.5, instruments=6)   # a count
        diag.remap_instruments({1: 1, 3: 2, 17: 3})
        w = diag.warnings[0]
        self.assertEqual((w["instrument"], w["extra_ctx"]), (3, "instrument 3"))
        self.assertEqual(diag.infos[0]["instruments"], [2, 3])
        self.assertEqual(diag.infos[1]["composites"], [(3, 4, "x"), (2, 2, "y")])
        self.assertEqual(diag.infos[2]["instruments"], 6)


if __name__ == "__main__":
    unittest.main()
