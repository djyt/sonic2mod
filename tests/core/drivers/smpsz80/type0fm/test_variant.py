"""SMPS Z80 Type 0 FM (Golden Axe): its flags and voices on hand-built bytes, then the ROM when it
is present.

    python -m pytest tests/core/drivers/smpsz80/type0fm/test_variant.py -q
"""

from __future__ import annotations

import dataclasses
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers import (
    detect_variant,
    read_rom_code,
    read_rom_song,
)
from core.drivers.reference import FM_FREQUENCIES
from core.drivers.smpsz80.memory import BankedZ80Memory
from core.drivers.smpsz80.type0fm import TYPE0FM
from core.drivers.smpsz80.type0fm.layout import HEADER_TYPE0, VOICE_TYPE0
from core.drivers.smpsz80.type0fm.locate import fm_table, locate_type0, sound_bank
from core.rom import RomError, RomImage
from core.rom.header import read_music_header
from core.rom.tracks import decode_tracks
from core.rom.voices import read_voices
from core.rom.z80 import z80_ram
from core.smps import (
    NO_TEMPO_HOLDS,
    CoordFlag,
    SongCode,
    VoiceField,
    played_song,
    song_from_code,
    source_names,
    write_asm,
)
from tests.roms import (
    GOLDEN_AXE_ROM,
    needs_golden_axe,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


class Type0Fm(unittest.TestCase):
    """Golden Axe's driver on a hand-built bank: its header, voice and flags."""

    _BANK = 0x8000

    def _memory(self, fm1: bytes) -> BankedZ80Memory:
        """A song at Z80 $8000: drums at $8020, FM1 at $8028, FM2 at $8040, one voice at $8048."""
        def z80(at: int) -> bytes:
            return (0x8000 + at).to_bytes(2, "little")

        bank = bytearray(0x100)
        bank[0:6] = z80(0x48) + bytes([3, 0, 2, 0])                       # 3 DAC/FM tracks, divider 2, tempo 0
        bank[6:18] = z80(0x20) + b"\0\0" + z80(0x28) + bytes([0xF4, 8]) + z80(0x40) + bytes([0, 10])
        bank[0x20:0x23] = bytes([0x81, 0x08, 0xF2])                        # drum 1
        bank[0x28:0x28 + len(fm1)] = fm1
        bank[0x40] = 0xF2
        bank[0x48:0x48 + 26] = bytes([0x3A, 0x80,                         # algorithm 2 feedback 7, pan left
                                      0x10, 0x20, 0x30, 0x7F,             # TL, register order
                                      0x71, 0x02, 0x03, 0x14]) + bytes(16)
        rom = bytearray(_HEADER.ljust(2 * self._BANK, b"\0"))
        rom[self._BANK:self._BANK + len(bank)] = bank
        return BankedZ80Memory(RomImage(bytes(rom)), self._BANK)

    def _code(self, fm1: bytes) -> SongCode:
        memory = self._memory(fm1)
        head = read_music_header(memory, self._BANK, HEADER_TYPE0)
        tracks = decode_tracks(memory, head.tracks, TYPE0FM)
        voices = read_voices(memory, head.voices, 1, VOICE_TYPE0)
        return SongCode(head.header, tracks.code, voices, TYPE0FM.rules, dropped=dict(tracks.dropped))

    def test_the_drums_play_on_fm3_and_tempo_0_never_holds(self):
        header = self._code(bytes([0xF2])).header
        song = song_from_code(header, self._code(bytes([0xF2])).code, [], TYPE0FM.rules)
        self.assertEqual([c.channel_type for c in header.channels], ["DAC", "FM", "FM"])
        self.assertEqual(source_names(song), ["FM3", "FM1", "FM2"])
        self.assertEqual((header.tempo_divider, header.tempo_modifier), (2, NO_TEMPO_HOLDS))
        self.assertEqual((header.channels[1].pitch_offset, header.channels[1].volume), (-12, 8))

    def test_the_voice_stores_its_pan_and_tl_first(self):
        voice = self._code(bytes([0xF2])).voices[0]
        self.assertEqual((voice.algorithm, voice.feedback, voice.pan), (2, 7, 0x80))
        self.assertEqual(voice.operators[VoiceField.TOTAL_LEVEL], (0x7F, 0x30, 0x20, 0x10))
        self.assertEqual(voice.operators[VoiceField.MULTIPLE], (4, 3, 2, 1))

    def test_f0_sets_the_volume_fb_transposes_and_an_unhandled_flag_skips_one_byte(self):
        code = self._code(bytes([0xEF, 0x00, 0xF0, 0x10, 0xA0, 0x08, 0xFB, 0x0C, 0xE0, 0x55, 0xA0, 0x08, 0xF2]))
        song = code.song()
        fm1 = song.channels[1]
        effects = [(e.effect.flag, list(e.effect.values)) for e in fm1.events if e.effect is not None]
        self.assertEqual(effects, [(CoordFlag.SET_VOICE, [0]), (CoordFlag.PAN, [0x80]), (CoordFlag.SET_VOL, [0x10]),
                                   (CoordFlag.CHANGE_TRANSPOSITION, [12])])
        self.assertEqual([(e.note.note_value, e.note.duration) for e in fm1.events if e.note is not None],
                         [(0xA0, 16), (0xA0, 16)])                       # durations x divider 2
        self.assertEqual(code.dropped, {"$E0 (no handler)": 1})

    def test_notes_play_from_the_songs_own_fm_table(self):
        code = self._code(bytes([0xEF, 0x00, 0xA0, 0x08, 0xF2]))           # $A0: table index $20
        table = tuple(range(0x1000, 0x1000 + 0x60))
        code.rules = dataclasses.replace(code.rules, fm_frequencies=table)
        fm1 = played_song(code.song()).channels["FM1"]
        played = next(p for p in fm1 if not p.rest)
        self.assertEqual(played.note, table[0x20 - 12])                   # transposition -12

    def test_slide_mode_is_refused_and_so_is_the_asm(self):
        with self.assertRaisesRegex(RomError, "slide mode"):
            self._code(bytes([0xFC, 0x01, 0xA0, 0x00, 0x00, 0x08, 0xF2]))
        with self.assertRaisesRegex(ValueError, "no SMPS2ASM spelling"):
            write_asm(self._code(bytes([0xF2])), "Mus81")


@needs_golden_axe
class GoldenAxe(unittest.TestCase):
    """SMPS Z80 Type 0 FM, no disassembly: what docs/todo/binary_import.md's Phase 3 probe found."""

    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(GOLDEN_AXE_ROM)

    def test_the_driver_and_its_bank(self):
        self.assertEqual(fm_table(z80_ram(self.rom)), 0x07D9)
        self.assertEqual(sound_bank(self.rom), 0x18000)

    def test_the_driver_is_pinned_and_every_song_reads(self):
        self.assertIs(detect_variant(self.rom), TYPE0FM)
        index = locate_type0(self.rom)
        songs = {sid: read_rom_song(self.rom, sid, index) for sid in index.music}
        self.assertEqual(source_names(songs[0x81]), ["FM3", "FM1", "FM2", "FM4", "FM5", "FM6"])
        self.assertEqual(songs[0x85].header.tempo_modifier, NO_TEMPO_HOLDS)          # Death Adder: tempo 0
        self.assertTrue(all(v.pan is not None for v in songs[0x81].voices))
        self.assertEqual(songs[0x81].rules.fm_frequencies[1:3], (0x283, 0x2A4))             # nC0: Z80 $07D9
        self.assertEqual(len(songs[0x81].rules.fm_frequencies), len(FM_FREQUENCIES))

    def test_the_drum_kit(self):
        drums = read_rom_song(self.rom, 0x81).fm_drums                 # Wilderness: tempo 10
        self.assertEqual(sorted(drums), [f"drum{0x80 + n:02X}" for n in range(1, 15)])
        kick = drums["drum81"]
        self.assertEqual([f.word for f in kick.frames[:4]], [0x1474, 0x1388, 0x1ACF, 0x12CF])  # a tie chain down
        self.assertTrue(drums["drum89"].silent)                       # a rest: a hit only stops the drum before
        self.assertIn("no stop", drums["drum8A"].cut)                  # runs on into voice data

    def test_sfx_with_slides_or_fm3_special_mode_are_refused(self):
        index = locate_type0(self.rom)
        refused: Counter[str] = Counter()
        for sid in index.sfx:
            try:
                read_rom_code(self.rom, sid, index)
            except RomError as e:
                refused[re.sub(r"^\$[0-9A-F]+: ", "", str(e)).split(" ")[0]] += 1
        self.assertEqual(refused, {"$FC": 20, "$FE": 1})

    def test_the_indexes_by_structure(self):
        index = locate_type0(self.rom)
        self.assertEqual((len(index.music), index.music[0x81], index.music[0x8F]), (15, 0x1937F, 0x1D1CD))
        self.assertEqual((len(index.sfx), min(index.sfx), max(index.sfx)), (42, 0x90, 0xB9))


if __name__ == "__main__":
    unittest.main()
