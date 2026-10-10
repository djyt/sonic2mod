"""The conversion's warnings and infos (core/diagnostics.py), the report that prints them, and
their instrument numbers after the slots are compacted (Diagnostics.remap_instruments).

    python -m pytest tests/core/test_diagnostics.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.diagnostics import Diagnostics, InfoKind, WarningKind
from core.ui.report import _WARNINGS


class Kinds(unittest.TestCase):
    def test_every_warning_has_a_report_line(self):
        # A kind without one prints as a raw dict under "other"
        self.assertEqual(set(_WARNINGS), set(WarningKind))

    def test_a_kind_is_its_string(self):
        self.assertEqual(WarningKind.CLAMP_HIGH, 'clamp_high')
        self.assertEqual(f"{InfoKind.LOOP_SET}", 'loop_set')


class Record(unittest.TestCase):
    def test_a_warning_is_kept_once_per_channel_context_and_note(self):
        d = Diagnostics()
        d.warn(WarningKind.CLAMP_HIGH, channel='FM1', src_name='C7')
        d.warn(WarningKind.CLAMP_HIGH, channel='FM1', src_name='C7', boundary='B6')
        d.warn(WarningKind.CLAMP_HIGH, channel='FM2', src_name='C7')
        self.assertEqual([w['channel'] for w in d.warnings], ['FM1', 'FM2'])
        self.assertEqual(d.warnings[0], {'type': WarningKind.CLAMP_HIGH, 'channel': 'FM1', 'src_name': 'C7'})

    def test_a_field_cannot_overwrite_the_kind(self):
        # A spread dict with its own 'type' used to turn tempo_no_slot into tempo_change
        with self.assertRaises(AssertionError):
            Diagnostics().warn(WarningKind.TEMPO_NO_SLOT, **{'type': 'tempo_change'})

    def test_infos_by_kind(self):
        d = Diagnostics()
        d.info(InfoKind.NARROWED, before=10, after=4)
        d.info(InfoKind.LOOP_SET, pattern=3)
        d.info(InfoKind.NARROWED, before=8, after=4)
        self.assertEqual(d.first_info(InfoKind.LOOP_SET), {'type': InfoKind.LOOP_SET, 'pattern': 3})
        self.assertEqual([i['before'] for i in d.infos_of(InfoKind.NARROWED)], [10, 8])
        self.assertIsNone(d.first_info(InfoKind.MERGE_BANK))


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
