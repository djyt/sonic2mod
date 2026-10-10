"""Data fixes spliced into a ROM's bytes (core/rom/fixes.py).

    python -m pytest tests/core/rom/test_fixes.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.sonic1 import SONIC1
from core.rom import RomError, RomFix, RomImage
from core.rom.fixes import apply_fixes
from core.rom.tracks import decode_tracks
from core.smps import (
    ChannelType,
    OpKind,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


_SONG = 0x200          # where the hand-built songs start


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


def _memory(song: bytes) -> Relative68kMemory:
    """The hand-built ROM as the 68k drivers read it."""
    return Relative68kMemory(_rom(song))


def _music(tracks: list[bytes], tempo: tuple[int, int] = (1, 3), voices: bytes = b"") -> bytes:
    """A music header with a DAC track and FM tracks, the code after it, then the voices."""
    fixed = 6 + 4 * len(tracks)
    offsets, at = [], fixed
    for t in tracks:
        offsets.append(at)
        at += len(t)
    head = (at if voices else 0).to_bytes(2, "big") + bytes([len(tracks), 0, *tempo])
    head += b"".join(o.to_bytes(2, "big") + bytes([0xF4, 0x08]) for o in offsets)
    return head + b"".join(tracks) + voices


class Fixes(unittest.TestCase):
    def test_a_splice_reads_the_original_bytes_as_the_replacement(self):
        fm = bytes([0xA0, 0x06, 0x80, 0x80, 0xE6, 0x0C, 0xB0, 0x06, 0xF2])
        fix = RomFix(_SONG + 12, bytes([0x80, 0x80, 0xE6, 0x0C]), b"", "test")
        code = decode_tracks(_memory(_music([fm])), {_SONG + 10: ChannelType.FM}, SONIC1, {fix.address: fix}).code
        self.assertEqual([op.value for op in code.ops if op.kind in (OpKind.NOTE, OpKind.DURATION)], [0xA0, 0x06, 0xB0, 0x06])
        self.assertFalse(any(op.kind is OpKind.EFFECT for op in code.ops))

    def test_a_fix_whose_bytes_differ_is_refused(self):
        with self.assertRaises(RomError):
            apply_fixes(_rom(b"\x01"), (RomFix(_SONG, b"\x02", b"\x03", "test"),))


if __name__ == "__main__":
    unittest.main()
