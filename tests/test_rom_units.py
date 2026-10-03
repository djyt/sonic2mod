"""ROM input (core/rom/) on hand-built bytes, and the parser split it rests on (core/smps/code.py).
The last class reads the real ROM and the disassembly when both are present.

    python -m pytest tests -q
"""

from __future__ import annotations

import glob
import re
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

from core.rom import RomError, RomFix, RomImage, dac_samples, data_fixes, locate_sounds, read_rom_code, read_rom_song
from core.rom.fixes import apply_fixes
from core.rom.header import read_music_header, read_sfx_header
from core.rom.kosinski import kosinski
from core.rom.tracks import decode_tracks
from core.rom.voices import read_voices
from core.smps import (
    CoordFlag,
    OpKind,
    SmpsParser,
    VoiceField,
    effect_from_bytes,
    parse_differences,
    write_asm,
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


class Image(unittest.TestCase):
    def test_reads_are_big_endian_and_bounded(self):
        rom = _rom(bytes([0x12, 0x34, 0xFF, 0xFE]))
        self.assertEqual(rom.word(_SONG), 0x1234)
        self.assertEqual(rom.signed_word(_SONG + 2), -2)
        with self.assertRaises(RomError):
            rom.word(_SONG + 3)

    def test_a_file_without_the_header_is_refused(self):
        path = Path(self.id().replace(".", "_") + ".bin")
        path.write_bytes(b"\0" * 0x200)
        try:
            with self.assertRaises(RomError):
                RomImage.load(path)
        finally:
            path.unlink()


class Tracks(unittest.TestCase):
    def test_a_backward_jump_is_the_loop(self):
        # DAC: rest $10, stop.  FM1: note $0C, jump back to its own note
        dac = bytes([0x80, 0x10, 0xF2])
        fm_at = 6 + 8 + len(dac)
        fm = bytes([0xA0, 0x0C, 0xF6]) + _pointer(fm_at + 3, fm_at)
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index())
        fm1 = song.channels[1]
        self.assertTrue(fm1.has_jump)
        self.assertEqual((fm1.loop_tick, fm1.loop_event_index), (0, None))   # its own start: by tick, as the parser
        self.assertEqual([(e.note.note_value, e.note.duration) for e in fm1.events], [(0xA0, 0x0C)])

    def test_flags_take_their_operands_and_signed_ones_are_signed(self):
        dac = bytes([0xF0, 1, 2, 3, 4, 0xE9, 0xF4, 0xE6, 0x02, 0x80, 0x01, 0xF2])
        code, _ = decode_tracks(_rom(_music([dac])), {"dac": _SONG + 10})
        effects = [op.effect for op in code.ops if op.kind is OpKind.EFFECT]
        self.assertEqual([(e.flag, e.params) for e in effects],
                         [(CoordFlag.MOD_SET, [1, 2, 3, 4]), (CoordFlag.CHANGE_TRANSPOSITION, [-12]),
                          (CoordFlag.ALTER_VOL, [2])])

    def test_a_label_between_a_note_and_its_duration_leaves_the_duration_the_notes(self):
        # note $A0, then a loop target AT the duration byte $06: the note still lasts $06
        dac = bytes([0xF2])
        fm_at = 6 + 8 + len(dac)
        fm = bytes([0xA0, 0x06, 0xF7, 0x00, 0x02]) + _pointer(fm_at + 5, fm_at + 1) + bytes([0xF2])
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index())
        notes = [(e.note.note_value, e.note.duration, e.note.is_retrigger) for e in song.channels[1].events]
        # the replay re-reads $06 alone: a standalone duration re-keys the note
        self.assertEqual(notes, [(0xA0, 6, False), (0xA0, 6, True)])

    def test_a_call_is_inlined_and_returns(self):
        dac = bytes([0xF2])
        fm_at = 6 + 8 + len(dac)
        sub = fm_at + 5
        fm = bytes([0xF8]) + _pointer(fm_at + 1, sub) + bytes([0xF2, 0x00]) + bytes([0xB0, 0x04, 0xE3])
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index())
        self.assertEqual([e.note.note_value for e in song.channels[1].events], [0xB0])

    def test_an_unknown_flag_names_its_address(self):
        with self.assertRaisesRegex(RomError, r"\$20A: \$FB"):
            decode_tracks(_rom(_music([bytes([0xFB])])), {"x": _SONG + 10})

    def test_smpsFade_and_smpsStopSpecial_end_the_track(self):
        for flag in (0xE4, 0xEE):
            code, _ = decode_tracks(_rom(_music([bytes([0x80, 0x01, flag, 0xA0, 0x01])])), {"x": _SONG + 10})
            self.assertIs(code.ops[-1].kind, OpKind.STOP)


class Headers(unittest.TestCase):
    def test_music_tracks_are_relative_to_the_header(self):
        head = read_music_header(_rom(_music([b"\xF2", b"\xF2"], tempo=(2, 5))), _SONG)
        self.assertEqual([c.channel_type for c in head.header.channels], ["DAC", "FM"])
        self.assertEqual((head.header.tempo_divider, head.header.tempo_modifier), (2, 5))
        self.assertEqual(head.header.channels[1].pitch_offset, -12)
        self.assertEqual(sorted(head.tracks.values()), [_SONG + 14, _SONG + 15])

    def test_sfx_channels_carry_their_hardware_channel(self):
        sfx = bytes([0, 0, 1, 1, 0x80, 0xC0, 0, 10, 0xF4, 0x02, 0xF2])
        head = read_sfx_header(_rom(sfx), _SONG)
        ch = head.header.channels[0]
        self.assertTrue(head.header.is_sfx)
        self.assertEqual((ch.channel_type, ch.hw_channel, ch.pitch_offset, ch.volume), ("PSG", 0xC0, -12, 2))
        self.assertIsNone(head.voices)


class Voices(unittest.TestCase):
    def test_operator_groups_are_reversed_and_read_as_the_chip_reads_them(self):
        raw = bytes([0x3A,                      # unused 0, feedback 7, algorithm 2
                     0x71, 0x02, 0x03, 0x14,    # DT/MUL op4 op3 op2 op1
                     0x1F, 0x1F, 0x5F, 0x2F,    # KS/AR: op1 $2F has bit 5 set (AR written past 31)
                     0, 0, 0, 0x85,             # AM/D1R: op1 AM, D1R 5
                     0, 0, 0, 0, 0x0F, 0x1F, 0x2F, 0x3F,
                     0x80, 0x10, 0x20, 0x9F])   # TL: bit 7 dropped
        voice = read_voices(_rom(raw), _SONG, 1)[0]
        self.assertEqual((voice.algorithm, voice.feedback), (2, 7))
        self.assertEqual(voice.operators[VoiceField.MULTIPLE], (4, 3, 2, 1))
        self.assertEqual(voice.operators[VoiceField.DETUNE], (1, 0, 0, 7))
        self.assertEqual(voice.operators[VoiceField.ATTACK_RATE], (0xF, 0x1F, 0x1F, 0x1F))
        self.assertEqual(voice.operators[VoiceField.RATE_SCALE], (0, 1, 0, 0))
        self.assertEqual(voice.operators[VoiceField.AMP_MOD], (1, 0, 0, 0))
        self.assertEqual(voice.operators[VoiceField.TOTAL_LEVEL], (0x1F, 0x20, 0x10, 0x00))


class Fixes(unittest.TestCase):
    def test_a_splice_reads_the_original_bytes_as_the_replacement(self):
        fm = bytes([0xA0, 0x06, 0x80, 0x80, 0xE6, 0x0C, 0xB0, 0x06, 0xF2])
        rom = _rom(_music([fm]))
        fix = RomFix(_SONG + 12, bytes([0x80, 0x80, 0xE6, 0x0C]), b"", "test")
        code, _ = decode_tracks(rom, {"x": _SONG + 10}, {fix.address: fix})
        self.assertEqual([op.value for op in code.ops if op.kind is OpKind.BYTE], [0xA0, 0x06, 0xB0, 0x06])
        self.assertFalse(any(op.kind is OpKind.EFFECT for op in code.ops))

    def test_a_fix_whose_bytes_differ_is_refused(self):
        with self.assertRaises(RomError):
            apply_fixes(_rom(b"\x01"), (RomFix(_SONG, b"\x02", b"\x03", "test"),))


class Kosinski(unittest.TestCase):
    def test_literals_inline_copies_and_the_end_marker(self):
        # descriptor bits (from bit 0): 1 1 (literals A B), 0 0 1 1 (inline: count 3+2, offset -2), 0 1 (full)
        descriptor = 0b10_1100_11
        data = bytes([descriptor & 0xFF, descriptor >> 8, 0x41, 0x42, 0xFE, 0x00, 0xF8, 0x00])
        out, end = kosinski(data, 0)
        self.assertEqual(out, b"ABABABA")
        self.assertEqual(end, len(data))


class Effects(unittest.TestCase):
    def test_psg_voice_is_named_as_the_music_files_name_it(self):
        self.assertEqual(effect_from_bytes(CoordFlag.PSG_VOICE, [4]).params, ["fTone_04"])
        self.assertEqual(effect_from_bytes(CoordFlag.PSG_VOICE, [0]).params, ["$00"])
        self.assertEqual(effect_from_bytes(CoordFlag.DETUNE, [0xFD]).params, [-3])


class Parser(unittest.TestCase):
    _CREDITS_LIKE = """
Song_Header:
\tsmpsHeaderStartSong 1
\tsmpsHeaderChan      $01, $00
\tsmpsHeaderTempo     $01, $03
\tsmpsHeaderDAC       Song_DAC
Song_DAC:
\tdc.b\tnRst, $10
    if FixMusicAndSFXDataBugs=0
\tdc.b\tnRst, nRst
    endif
    if FixMusicAndSFXDataBugs
\tdc.b\tnRst
    else
\tdc.b\tnRst, nRst, nRst
    endif
\tsmpsStop
"""

    def test_the_data_fixes_read_both_conditional_forms(self):
        fixed = SmpsParser().parse_text(self._CREDITS_LIKE).channels[0].events
        shipped = SmpsParser(fix_data_bugs=False).parse_text(self._CREDITS_LIKE).channels[0].events
        self.assertEqual((len(fixed), len(shipped)), (2, 6))

    def test_smpsFade_ends_the_track(self):
        text = self._CREDITS_LIKE.replace("\tsmpsStop", "\tsmpsFade\n\tdc.b\tnRst, $20")
        self.assertEqual(len(SmpsParser().parse_text(text).channels[0].events), 2)


def _index():
    """The hand-built ROMs hold one song at _SONG: an index of it alone."""
    from core.rom import SoundIndex
    return SoundIndex(music={0x81: _SONG}, sfx={})


_ROM_FILE = ROOT / "input" / "roms" / "sonic_rev01.bin"
_ASM_DIR = ROOT / "sonic_1"


@unittest.skipUnless(_ROM_FILE.exists() and _ASM_DIR.exists(), "needs input/roms/sonic_rev01.bin and sonic_1/")
class SonicRev01(unittest.TestCase):
    """Every song and SFX of the ROM against the disassembly (data fixes off: the game as shipped)."""

    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(_ROM_FILE)
        cls.index = locate_sounds(cls.rom)

    def _asm(self):
        for path in sorted(glob.glob(str(_ASM_DIR / "music" / "*.asm")) + glob.glob(str(_ASM_DIR / "sfx" / "*.asm"))):
            yield int(re.search(r"(?:Mus|Snd)([0-9A-F]{2})", path).group(1), 16), path

    def test_the_indexes(self):
        self.assertEqual((len(self.index.music), len(self.index.sfx)), (19, 49))
        self.assertEqual(self.index.music[0x81], 0x745DC)

    def test_every_sound_reads_as_its_asm_fixed_or_shipped(self):
        for sound, path in self._asm():
            for fixed in (True, False):
                with self.subTest(path=Path(path).name, fixed=fixed):
                    want = SmpsParser(fix_data_bugs=fixed).parse_file(path)
                    got = read_rom_song(self.rom, sound, self.index, fix_data_bugs=fixed)
                    self.assertEqual(parse_differences(want, got), [])

    def test_the_data_fixes_are_this_roms_only(self):
        self.assertEqual(len(data_fixes(self.rom)), 3)
        other = RomImage(self.rom.data[:-1] + b"\0")
        self.assertEqual(data_fixes(other), ())

    def test_written_labels_are_named_and_keep_the_rom_address(self):
        text = write_asm(read_rom_code(self.rom, 0x81, self.index), "Mus81")
        self.assertIn("Mus81_FM1:              ; $7460C", text)
        self.assertIn("\tsmpsLoop            $00, $0D, Mus81_Loop00", text)

    def test_every_sound_survives_the_asm_round_trip(self):
        for sound, _ in self._asm():
            code = read_rom_code(self.rom, sound, self.index)
            with self.subTest(sound=f"${sound:02X}"):
                back = SmpsParser().parse_text(write_asm(code, f"S{sound:02X}"))
                self.assertEqual(parse_differences(code.song(), back), [])

    def test_the_dac_samples_are_samples_raws(self):
        raws = {"dKick": "kick", "dSnare": "snare", "dTimpani": "timpani"}
        for s in dac_samples(self.rom):
            if s.name in raws:
                self.assertEqual(s.pcm, (ROOT / "samples" / f"{raws[s.name]}.raw").read_bytes(), s.name)
        self.assertEqual([s.pitch for s in dac_samples(self.rom)], [23, 1, 27, 0x12, 0x15, 0x1C, 0x1D])


if __name__ == "__main__":
    unittest.main()
