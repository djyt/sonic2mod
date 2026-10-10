"""SMPS 68k Type 1a (Moonwalker): its flags on hand-built bytes, then the ROM when it is present.

    python -m pytest tests/core/drivers/smps68k/type1a/test_variant.py -q
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers import (
    dac_samples,
    detect_variant,
    first_failure,
    locate_sounds,
    read_rom_code,
    read_rom_song,
)
from core.drivers.reference import SONIC1_ENVELOPES
from core.drivers.smps68k.sonic1 import SONIC1
from core.drivers.smps68k.type1a import TYPE1A
from core.rom import RomError, RomImage
from core.smps import (
    CoordFlag,
    OpKind,
)
from tests.roms import (
    MOONWALKER_ROM,
    needs_moonwalker,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


_SONG = 0x200          # where the hand-built songs start


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


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


def _pointer(at: int, target: int) -> bytes:
    """A jump / call operand at song offset `at`: target = its address + 1 + signed word."""
    return (target - at - 1).to_bytes(2, "big", signed=True)


def _index():
    """The hand-built ROMs hold one song at _SONG: an index of it alone."""
    from core.rom import SoundIndex
    return SoundIndex(music={0x81: _SONG}, sfx={})


class Type1a(unittest.TestCase):
    """Moonwalker's driver: the same bytes, other flags."""

    def _song(self, fm: bytes):
        dac = bytes([0xF2])
        return read_rom_code(_rom(_music([dac, fm])), 0x81, _index(), variant=TYPE1A)

    def test_f9_returns_where_sonic1_writes_a_release_rate(self):
        fm_at = 6 + 8 + 1
        sub = fm_at + 4
        fm = bytes([0xF8]) + _pointer(fm_at + 1, sub) + bytes([0xF2]) + bytes([0xB0, 0x04, 0xF9])
        events = self._song(fm).song().channels[1].events
        self.assertEqual([e.note.note_value for e in events], [0xB0])

    def test_fb_transposes_and_fa_sets_the_tempo_divider(self):
        code = self._song(bytes([0xFB, 0x0C, 0xFA, 0x02, 0xA0, 0x01, 0xF2])).code
        effects = [(op.effect.flag, list(op.effect.values)) for op in code.ops if op.kind is OpKind.EFFECT]
        self.assertEqual(effects, [(CoordFlag.CHANGE_TRANSPOSITION, [12]), (CoordFlag.CHAN_TEMPO_DIV, [2])])

    def test_pan_animation_takes_four_more_operands_when_on_and_is_dropped(self):
        fm = bytes([0xE4, 0x00, 0xA0, 0x01, 0xE4, 0x01, 0x03, 0x00, 0x03, 0x0C, 0xA2, 0x01, 0xF2])
        song = self._song(fm)
        self.assertEqual([e.note.note_value for e in song.song().channels[1].events], [0xA0, 0xA2])
        self.assertEqual(song.dropped, {"pan animation": 2})

    def test_what_the_converter_cannot_render_is_refused(self):
        with self.assertRaisesRegex(RomError, "LFO"):
            self._song(bytes([0xE9, 0x08, 0x00, 0xF2]))

    def test_detection_takes_the_one_driver_every_song_decodes_with(self):
        type1a_style = _rom(_music([bytes([0xF2]), bytes([0xFB, 0x0C, 0xA0, 0x04, 0xF2])]))   # $FB: transposition
        self.assertIsNone(first_failure(type1a_style, _index(), TYPE1A))
        self.assertIsNotNone(first_failure(type1a_style, _index(), SONIC1))
        sonic1_style = _rom(_music([bytes([0xF2]), bytes([0xA0, 0x04, 0xE3])]))
        self.assertIsNone(first_failure(sonic1_style, _index(), SONIC1))
        self.assertIsNotNone(first_failure(sonic1_style, _index(), TYPE1A))


@needs_moonwalker
class Moonwalker(unittest.TestCase):
    """SMPS 68k Type 1a, no disassembly: what docs/todo/binary_import.md's probe found."""

    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(MOONWALKER_ROM)
        cls.index = locate_sounds(cls.rom)

    def test_the_indexes_by_structure(self):
        self.assertEqual(len(self.index.music), 23)
        self.assertEqual((self.index.music[0x81], self.index.music[0x97]), (0x63588, 0x67A10))
        self.assertEqual((len(self.index.sfx), max(self.index.sfx)), (49, 0xD3))

    def test_the_driver_is_type1a_pinned_or_tried(self):
        self.assertIs(detect_variant(self.rom), TYPE1A)
        self.assertIsNone(first_failure(self.rom, self.index, TYPE1A))
        self.assertIsNotNone(first_failure(self.rom, self.index, SONIC1))

    def test_every_song_reads(self):
        songs = {sid: read_rom_code(self.rom, sid, self.index) for sid in self.index.music}
        looping = sorted(sid for sid, code in songs.items() if all(c.has_jump for c in code.song().channels))
        self.assertEqual(looping, [0x81, 0x82, 0x83, 0x84, 0x85, 0x89, 0x8A])    # the VGZ pack's loops
        dropped = Counter()
        for code in songs.values():
            dropped.update(code.dropped)
        self.assertEqual(dropped, {"pan animation": 5, "queued sound (not part of the music)": 3})

    def test_its_own_envelopes_and_dac_names(self):
        song = read_rom_song(self.rom, 0x81, self.index)
        self.assertEqual(len(song.rules.psg_envelopes), 6)
        self.assertNotEqual(song.rules.psg_envelopes["fTone_03"], SONIC1_ENVELOPES["fTone_03"])
        self.assertEqual(len(song.rules.psg_envelopes["fTone_06"].steps), 16 + 41)     # runs on into envelope 5
        dac = {e.note.dac_name for e in song.channels[0].events if e.note and not e.note.is_rest}
        self.assertTrue(dac and all(name.startswith("dac") for name in dac))

    def test_the_dac_samples(self):
        samples = {s.sound: s for s in dac_samples(self.rom)}
        self.assertEqual([len(samples[b].pcm) for b in range(0x81, 0x86)], [1280, 4096, 1536, 4266, 3584])
        self.assertEqual((samples[0x8C].of, samples[0x8C].pitch, samples[0x90].pitch), (0x85, 0x16, 0x1E))
        self.assertEqual({b: round(samples[b].rate) for b in (0x81, 0x82, 0x84)},
                         {0x81: 10739, 0x82: 6770, 0x84: 15193})     # the rips: 10765, ~6770, ~15190


if __name__ == "__main__":
    unittest.main()
