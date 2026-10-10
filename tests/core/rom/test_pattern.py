"""Code patterns written as listings (core/rom/pattern.py).

    python -m pytest tests/core/rom/test_pattern.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.rom.pattern import code_pattern


class Listing(unittest.TestCase):
    def test_bytes_wildcards_and_captures_the_mnemonics_left_out(self):
        pattern = code_pattern("""
            3A <w>      ld a,(hi)
            CD ? ?      call
            0E <b>      ld c,n
            ?2          two bytes, any
            C9          ret
        """)
        found = pattern.search(b"\x00\x3A\x01\x1C\xCD\xFF\xFF\x0E\x0A\x3A\x0A\xC9")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual((found.start(), found.groups()), (1, (b"\x01\x1C", b"\x0A")))

    def test_any_byte_matches_a_newline_and_special_bytes_are_literal(self):
        self.assertIsNotNone(code_pattern("2A ?  ld hl,(nn)").fullmatch(b"\x2A\x0A"))
        self.assertIsNone(code_pattern("2E  ld l,n").fullmatch(b"\x00"))         # $2E is the regex's '.'

    def test_a_listing_of_no_bytes_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no bytes"):
            code_pattern("ld a,b")


if __name__ == "__main__":
    unittest.main()
