"""Streets of Rage 2's driver (core/drivers/mucom/md/): its image header, dialect and prefix on
hand-built bytes, then the ROM's index, tables and every song when it is present.

    python -m pytest tests/core/drivers/mucom/md/test_variant.py -q
"""

from __future__ import annotations

import unittest

from core.drivers import detect_variant, read_rom_code, read_rom_song
from core.drivers.mucom.md import MUCOM_MD
from core.drivers.mucom.md.header import read_image
from core.drivers.mucom.md.locate import fm_volumes, locate
from core.drivers.mucom.md.memory import MucomMemory
from core.rom import RomError, RomImage
from core.smps import (
    FIRST_NOTE,
    AdsrEnvelope,
    ChannelType,
    EnvelopePreset,
    ModSet,
    NoiseRate,
    Pan,
    PsgForm,
    SoftRelease,
)
from tests.roms import STREETS_OF_RAGE_2_ROM, needs_streets_of_rage_2

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_AT = 0x200
_CHANNELS = 11
_ENTRIES = 10 + 4 * _CHANNELS               # where the hand-built image's tracks start
_REST = bytes([0x86, 0x00])                 # a 6-frame rest, the end


def _memory(code: bytes) -> MucomMemory:
    return MucomMemory(RomImage(_HEADER + code))


def _image(tracks: dict[int, bytes], loops: dict[int, int] | None = None) -> bytes:
    """An image whose channel i (0 = A) holds tracks[i] (else a lone $00), looping to its offset
    loops[i] into its code; the voices after them."""
    loops = loops or {}
    entries, code = b"", b""
    for i in range(_CHANNELS):
        start = _ENTRIES + len(code) - 9                     # from +9
        loop = start + loops[i] if i in loops else 0
        entries += start.to_bytes(2, "little") + loop.to_bytes(2, "little")
        code += tracks.get(i, b"\0")
    voices = _ENTRIES + len(code) - 5
    return bytes([0x00, 0xC2, 0, 0, 0]) + voices.to_bytes(2, "little") + bytes([0, 0, 0]) + entries + code


def _read(code: bytes, kind: ChannelType = ChannelType.FM):
    return MUCOM_MD.grammar(_memory(code), _AT, MUCOM_MD, kind)


def _effect(code: bytes, kind: ChannelType = ChannelType.FM):
    """The one effect `code` reads as, whole."""
    read = _read(code, kind)
    assert read.length == len(code) and len(read.ops) == 1
    return read.ops[0].effect


class Image(unittest.TestCase):
    def test_nine_tracks_in_streets_of_rages_order(self):
        head = read_image(_memory(_image({0: _REST})), _AT)
        self.assertEqual([c.chip_channel or c.channel_type.value for c in head.header.channels],
                         ["FM1", "FM2", "FM3", "FM4", "FM5", "DAC", "PSG1", "PSG2", "PSG3"])
        self.assertEqual((head.header.fm_count, head.header.psg_count), (6, 3))

    def test_a_track_loops_where_its_entry_says(self):
        head = read_image(_memory(_image({0: bytes([0xF1, 0x0A]) + _REST}, {0: 2})), _AT)
        start = _AT + _ENTRIES
        self.assertEqual(head.loops, {start: start + 2})

    def test_an_empty_track_does_not_loop(self):
        self.assertEqual(read_image(_memory(_image({}, {0: 0})), _AT).loops, {})

    def test_rhythm_and_fm6_must_be_empty(self):
        for channel in (6, 9):
            with self.assertRaisesRegex(RomError, "never plays"):
                read_image(_memory(_image({channel: _REST})), _AT)

    def test_not_an_image(self):
        with self.assertRaisesRegex(RomError, "not a MUCOM88 song image"):
            read_image(_memory(bytes(_ENTRIES + 8)), _AT)


class Dialect(unittest.TestCase):
    def test_psg_notes_from_a2_the_clamped_ones_at_it(self):
        notes = {byte: _read(bytes([6, byte]), ChannelType.PSG).ops[0].value - FIRST_NOTE
                 for byte in (0x19, 0x10, 0x00, 0x30, 0x7B)}
        self.assertEqual(notes, {0x19: 0, 0x10: 0, 0x00: 0, 0x30: 15, 0x7B: 74})

    def test_pan_by_ror_2(self):
        self.assertEqual([_read(bytes([0xF8, n])).ops[0].effect for n in range(4)],
                         [Pan(0x00), Pan(0x40), Pan(0x80), Pan(0xC0)])

    def test_the_prefix_sets_the_soft_release(self):
        self.assertEqual(_effect(bytes([0xFF, 0xF3, 4])), SoftRelease(4, on=True))
        self.assertEqual(_effect(bytes([0xFF, 0xF5, 0])), SoftRelease(None, on=False))
        self.assertEqual(_effect(bytes([0xFF, 0xF5, 1])), SoftRelease(None, on=True))
        self.assertEqual(_read(bytes([0xFF, 0xF2, 1, 2])).length, 4)
        with self.assertRaisesRegex(RomError, "a rest keys off"):
            _read(bytes([0xFF, 0xF4, 1]))
        with self.assertRaisesRegex(RomError, "prefix table"):
            _read(bytes([0xFF, 0xF6, 0]))

    def test_psg_noise_takes_the_channel_and_gives_it_back(self):
        self.assertEqual(_effect(bytes([0xF7, 3]), ChannelType.PSG), PsgForm(0xE4))
        self.assertEqual(_effect(bytes([0xF7, 8]), ChannelType.PSG), PsgForm(None))
        self.assertEqual([_effect(bytes([0xF8, n]), ChannelType.PSG) for n in (11, 12, 22, 23)],
                         [NoiseRate(0), NoiseRate(1), NoiseRate(1), NoiseRate(2)])

    def test_psg_envelopes_a_preset_or_six_bytes(self):
        self.assertEqual(_effect(bytes([0xF0, 2]), ChannelType.PSG), EnvelopePreset(2))
        self.assertEqual(_effect(bytes([0xFA, 200, 50, 255, 220, 1, 10]), ChannelType.PSG),
                         AdsrEnvelope(200, 50, 255, 220, 1, 10))

    def test_psg_vibrato_depth_scaled_266_256(self):
        mod = _effect(bytes([0xF4, 0, 1, 1, 0xD0, 0x07, 4]), ChannelType.PSG)       # depth 2000
        self.assertEqual(mod, ModSet(1, 1, 2000 * 266 >> 8, 4))

    def test_the_drum_track_skips_what_plays_no_sample(self):
        vibrato = _read(bytes([0xF4, 0, 1, 2, 3, 0, 4]), ChannelType.DAC)
        self.assertEqual((vibrato.ops, vibrato.length), ((), 7))
        self.assertEqual(_read(bytes([0xFF, 0xF4, 1]), ChannelType.DAC).length, 3)


@needs_streets_of_rage_2
class StreetsOfRage2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(STREETS_OF_RAGE_2_ROM)
        cls.index = locate(cls.rom)

    def test_pinned_to_its_driver(self):
        self.assertIs(detect_variant(self.rom), MUCOM_MD)

    def test_the_index_songs_from_0_sfx_from_20(self):
        self.assertEqual((min(self.index.music), max(self.index.music), self.index.music[0]), (0x00, 0x1B, 0x42BCE))
        self.assertEqual((min(self.index.sfx), max(self.index.sfx), len(self.index.sfx)), (0x20, 0x56, 55))

    def test_tables(self):
        rules = read_rom_song(self.rom, 0, self.index).rules
        self.assertEqual(fm_volumes(self.rom)[:4], (0x36, 0x33, 0x30, 0x2D))
        self.assertEqual((rules.psg_table.pitch, rules.psg_table.notes), (33, 75))      # A2 .. B8
        self.assertEqual((rules.psg_frequencies[0], rules.psg_frequencies[-1]), (1016, 14))
        self.assertEqual(rules.fm_frequencies[1 + 12 * 4 + 9], 0x2000 | 0x43C)          # A4: block 4

    def test_every_song_loops_but_the_one_shot(self):
        once = [sid for sid in self.index.music
                if not any(ch.has_jump for ch in read_rom_song(self.rom, sid, self.index).channels)]
        self.assertEqual(once, [0x19])

    def test_a_carrier_write_holds_a_level_the_volume_then_resets(self):
        song = read_rom_song(self.rom, 0x02, self.index)
        held = [v for v in song.voices if v.volume_slots is not None]
        self.assertTrue(held)
        self.assertTrue(all(len(v.volume_slots or ()) < len(v.carrier_registers) for v in held))

    def test_every_flag_kept_with_its_bytes_and_reading(self):
        flags = read_rom_code(self.rom, 0x06, self.index).flags
        release = next(f for f in flags if f.address == 0x47648)
        self.assertEqual((release.kind, release.data, release.read),
                         (ChannelType.FM, bytes([0xFF, 0xF3, 4]), "SoftRelease(level=4, on=True)"))
        self.assertEqual([f.address for f in flags], sorted(f.address for f in flags))


if __name__ == "__main__":
    unittest.main()
