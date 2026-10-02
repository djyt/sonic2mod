"""The conversion's warnings and infos (core/diagnostics.py) and the report that prints them.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.diagnostics import Diagnostics, InfoKind, WarningKind
from core.report import _WARNINGS


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


if __name__ == "__main__":
    unittest.main()
