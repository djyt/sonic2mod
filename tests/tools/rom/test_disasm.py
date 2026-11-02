"""disasm.py's --find (tools/rom/disasm.py).

    python -m pytest tests/tools/rom/test_disasm.py -q
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.rom.disasm import capture_text, find_pattern, main

_CODE = b"\x00\x3A\x01\x80\x47\x0E\x3F\x3A\x05\x80\x47\x0E\x10"
_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_68K = bytes.fromhex("303C1234 4E75")                # move.w #$1234,d0 / rts


class Pattern(unittest.TestCase):
    def test_semicolons_break_lines_and_comments_stay_out(self):
        pattern = find_pattern("3A <w>  ld a,(hi); 47  ld b,a ; 0E <b>  ld c,n")
        self.assertEqual([m.start() for m in pattern.finditer(_CODE)], [1, 7])

    def test_captures_a_word_in_the_cpus_byte_order(self):
        match = find_pattern("3A <w>; 47; 0E <b>").search(_CODE)
        assert match is not None
        self.assertEqual(capture_text(match, "little"), "$8001 $3F")
        self.assertEqual(capture_text(match, "big"), "$0180 $3F")

    def test_no_captures_no_text(self):
        match = find_pattern("47").search(_CODE)
        assert match is not None
        self.assertEqual(capture_text(match, "little"), "")

    def test_a_listing_of_no_bytes_is_refused(self):
        with self.assertRaises(ValueError):
            find_pattern("ld a,(hl)")


class Cli(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.rom = Path(folder.name) / "code.bin"
        self.rom.write_bytes(_HEADER + _68K + _68K)

    def _run(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main([str(self.rom), *args])
        return code, out.getvalue()

    def test_every_match_its_captures_and_instructions(self):
        code, out = self._run("--find", "30 3C <w>  move.w #n,d0", "--count", "2")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["2 matches",
                                            "--- $000200  $1234", "  000200  move.w #$1234, d0", "  000204  rts",
                                            "--- $000206  $1234", "  000206  move.w #$1234, d0", "  00020A  rts"])

    def test_no_match_exits_1(self):
        self.assertEqual(self._run("--find", "4E 71"), (1, "no match\n"))

    def test_a_pattern_of_no_bytes_exits_2(self):
        self.assertEqual(self._run("--find", "nop")[0], 2)
