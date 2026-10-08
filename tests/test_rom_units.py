"""ROM input (core/rom/) on hand-built bytes, and the parser split it rests on (core/smps/code.py).
The last class reads the real ROM and the disassembly when both are present.

    python -m pytest tests -q
"""

from __future__ import annotations

import dataclasses
import glob
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_HERE))

from roms import MOONWALKER_ROM, needs_moonwalker

from core.chips import OperatorReg
from core.rom import (
    RomError,
    RomFix,
    RomImage,
    dac_samples,
    data_fixes,
    detect_variant,
    locate_sounds,
    read_rom_code,
    read_rom_song,
)
from core.rom.detect import first_failure
from core.rom.envelopes import read_envelopes
from core.rom.fixes import apply_fixes
from core.rom.header import read_music_header, read_sfx_header
from core.rom.smps68k import SONIC1, TYPE1A
from core.rom.smps68k.kosinski import kosinski
from core.rom.smps68k.memory import Relative68kMemory
from core.rom.smpsz80 import TYPE0FM
from core.rom.smpsz80.drums import _Player, _wrap
from core.rom.smpsz80.layout import HEADER_TYPE0, VOICE_TYPE0
from core.rom.smpsz80.locate import fm_table, locate_type0, sound_bank
from core.rom.smpsz80.memory import BankedZ80Memory, Z80RamMemory
from core.rom.tracks import decode_tracks
from core.rom.variant import EntryLayout, VoiceLayout
from core.rom.voices import read_voices
from core.rom.z80 import z80_ram
from core.smps import (
    FM_FREQUENCIES,
    NO_TEMPO_HOLDS,
    SONIC1_ENVELOPES,
    ChannelType,
    CoordFlag,
    OpKind,
    PsgEnvelope,
    SmpsParser,
    SongCode,
    VoiceField,
    effect_from_bytes,
    noise_envelope_frames,
    parse_differences,
    played_song,
    song_from_code,
    source_names,
    tempo_schedule,
    write_asm,
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

    def test_a_shift_jis_title_reads_as_ascii(self):
        title = "ＧＯＬＤＥＮ　ＡＸＥ".encode("shift_jis")
        data = bytearray(_HEADER)
        data[0x150:0x150 + len(title)] = title
        self.assertEqual(RomImage(bytes(data)).title, "GOLDEN AXE")

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
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index(), variant=SONIC1)
        fm1 = song.channels[1]
        self.assertTrue(fm1.has_jump)
        self.assertEqual((fm1.loop_tick, fm1.loop_event_index), (0, None))   # its own start: by tick, as the parser
        self.assertEqual([(e.note.note_value, e.note.duration) for e in fm1.events], [(0xA0, 0x0C)])

    def test_flags_take_their_operands_and_signed_ones_are_signed(self):
        dac = bytes([0xF0, 1, 2, 3, 4, 0xE9, 0xF4, 0xE6, 0x02, 0x80, 0x01, 0xF2])
        code = decode_tracks(_memory(_music([dac])), {_SONG + 10: ChannelType.DAC}, SONIC1).code
        effects = [op.effect for op in code.ops if op.kind is OpKind.EFFECT]
        self.assertEqual([(e.flag, e.params) for e in effects],
                         [(CoordFlag.MOD_SET, [1, 2, 3, 4]), (CoordFlag.CHANGE_TRANSPOSITION, [-12]),
                          (CoordFlag.ALTER_VOL, [2])])

    def test_a_label_between_a_note_and_its_duration_leaves_the_duration_the_notes(self):
        # note $A0, then a loop target AT the duration byte $06: the note still lasts $06
        dac = bytes([0xF2])
        fm_at = 6 + 8 + len(dac)
        fm = bytes([0xA0, 0x06, 0xF7, 0x00, 0x02]) + _pointer(fm_at + 5, fm_at + 1) + bytes([0xF2])
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index(), variant=SONIC1)
        notes = [(e.note.note_value, e.note.duration, e.note.is_retrigger) for e in song.channels[1].events]
        # the replay re-reads $06 alone: a standalone duration re-keys the note
        self.assertEqual(notes, [(0xA0, 6, False), (0xA0, 6, True)])

    def test_a_call_is_inlined_and_returns(self):
        dac = bytes([0xF2])
        fm_at = 6 + 8 + len(dac)
        sub = fm_at + 5
        fm = bytes([0xF8]) + _pointer(fm_at + 1, sub) + bytes([0xF2, 0x00]) + bytes([0xB0, 0x04, 0xE3])
        song = read_rom_song(_rom(_music([dac, fm])), 0x81, _index(), variant=SONIC1)
        self.assertEqual([e.note.note_value for e in song.channels[1].events], [0xB0])

    def test_an_unknown_flag_names_its_address(self):
        with self.assertRaisesRegex(RomError, r"\$20A: \$FB"):
            decode_tracks(_memory(_music([bytes([0xFB])])), {_SONG + 10: ChannelType.FM}, SONIC1)

    def test_code_two_kinds_share_needs_one_flag_table(self):
        psg = {**SONIC1.flags[ChannelType.PSG]}
        variant = dataclasses.replace(SONIC1, flags={**SONIC1.flags, ChannelType.PSG: psg})
        memory = _memory(_music([bytes([0x80, 0x01, 0xF2])]))
        with self.assertRaisesRegex(RomError, r"\$20C: code shared by PSG and FM tracks"):
            decode_tracks(memory, {_SONG + 10: ChannelType.FM, _SONG + 12: ChannelType.PSG}, variant)
        decode_tracks(memory, {_SONG + 10: ChannelType.FM, _SONG + 12: ChannelType.PSG}, SONIC1)

    def test_smpsFade_and_smpsStopSpecial_end_the_track(self):
        for flag in (0xE4, 0xEE):
            code = decode_tracks(_memory(_music([bytes([0x80, 0x01, flag, 0xA0, 0x01])])), {_SONG + 10: ChannelType.FM}, SONIC1).code
            self.assertIs(code.ops[-1].kind, OpKind.STOP)


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
        effects = [(op.effect.flag, op.effect.params) for op in code.ops if op.kind is OpKind.EFFECT]
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


class Envelopes(unittest.TestCase):
    def test_a_held_envelope_gives_its_steps_a_looping_one_repeats(self):
        self.assertEqual(PsgEnvelope((0, 1, 2)).frames(8), [0, 1, 2])
        self.assertEqual(PsgEnvelope((0, 1, 2), loop_to=1).frames(7), [0, 1, 2, 1, 2, 1, 2])
        self.assertIsNone(noise_envelope_frames(PsgEnvelope((0, 1), loop_to=0)))
        self.assertEqual(noise_envelope_frames(PsgEnvelope((0, 13))), 2 + 2 + 1)

    def test_each_drivers_commands_end_an_envelope(self):
        # Three envelopes: hold, restart, jump to step 1
        data = bytes([0x00, 0x01, 0x83, 0x02, 0x03, 0x80, 0x04, 0x05, 0x06, 0x85, 0x01])
        addresses = (_SONG, _SONG + 3, _SONG + 6)
        envelopes = read_envelopes(_memory(data), addresses, TYPE1A)
        self.assertEqual(envelopes, {"fTone_01": PsgEnvelope((0, 1)), "fTone_02": PsgEnvelope((2, 3), 0),
                                     "fTone_03": PsgEnvelope((4, 5, 6), 1)})
        with self.assertRaisesRegex(RomError, r"\$83"):
            read_envelopes(_memory(data), addresses, SONIC1)       # Sonic 1 knows only $80 (hold)


class Headers(unittest.TestCase):
    def test_music_tracks_are_relative_to_the_header(self):
        head = read_music_header(_memory(_music([b"\xF2", b"\xF2"], tempo=(2, 5))), _SONG, SONIC1.header)
        self.assertEqual([c.channel_type for c in head.header.channels], ["DAC", "FM"])
        self.assertEqual((head.header.tempo_divider, head.header.tempo_modifier), (2, 5))
        self.assertEqual(head.header.channels[1].pitch_offset, -12)
        self.assertEqual(head.tracks, {_SONG + 14: ChannelType.DAC, _SONG + 15: ChannelType.FM})

    def test_a_layout_without_tempo_reads_its_own_entries(self):
        # voices.w fm.b psg.b, FM ptr.w volume.b, PSG ptr.w volume.b envelope.b: Streets of Rage's shape
        layout = dataclasses.replace(SONIC1.header, tempo=False, fm_entry=EntryLayout(3, volume=2),
                                     psg_entry=EntryLayout(4, volume=2, envelope=3))
        song = bytes([0, 0, 2, 1]) + bytes([0, 11, 4]) + bytes([0, 12, 5]) + bytes([0, 13, 7, 3]) + bytes([0xF2] * 3)
        head = read_music_header(_memory(song), _SONG, layout)
        _dac, fm, psg = head.header.channels
        self.assertEqual((head.header.tempo_divider, head.header.tempo_modifier), (1, NO_TEMPO_HOLDS))
        self.assertEqual((fm.volume, fm.pitch_offset, psg.volume, psg.psg_voice_label), (5, 0, 7, "fTone_03"))
        self.assertEqual(head.tracks, {_SONG + 11: ChannelType.DAC, _SONG + 12: ChannelType.FM, _SONG + 13: ChannelType.PSG})

    def test_sfx_channels_carry_their_hardware_channel(self):
        sfx = bytes([0, 0, 1, 1, 0x80, 0xC0, 0, 10, 0xF4, 0x02, 0xF2])
        head = read_sfx_header(_memory(sfx), _SONG, SONIC1.header)
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
        voice = read_voices(_memory(raw), _SONG, 1, SONIC1.voice_layout)[0]
        self.assertEqual((voice.algorithm, voice.feedback), (2, 7))
        self.assertEqual(voice.operators[VoiceField.MULTIPLE], (4, 3, 2, 1))
        self.assertEqual(voice.operators[VoiceField.DETUNE], (1, 0, 0, 7))
        self.assertEqual(voice.operators[VoiceField.ATTACK_RATE], (0xF, 0x1F, 0x1F, 0x1F))
        self.assertEqual(voice.operators[VoiceField.RATE_SCALE], (0, 1, 0, 0))
        self.assertEqual(voice.operators[VoiceField.AMP_MOD], (1, 0, 0, 0))
        self.assertEqual(voice.operators[VoiceField.TOTAL_LEVEL], (0x1F, 0x20, 0x10, 0x00))

    def test_register_order_with_feedback_last(self):
        # Streets of Rage's shape: each group in register order (+0 +4 +8 +C), B0 last
        groups = (OperatorReg.DT_MUL, OperatorReg.TL, OperatorReg.KS_AR, OperatorReg.AM_D1R,
                  OperatorReg.D2R, OperatorReg.D1L_RR)
        layout = VoiceLayout(groups, feedback_last=True, operator_offsets=(0x00, 0x04, 0x08, 0x0C))
        raw = bytes([1, 2, 3, 4, 0x11, 0x12, 0x13, 0x14, *[0x1F] * 16, 0x3A])
        voice = read_voices(_memory(raw), _SONG, 1, layout)[0]
        regs = voice.registers()
        self.assertEqual((voice.algorithm, voice.feedback), (2, 7))
        self.assertEqual([regs[OperatorReg.DT_MUL + off] for off in (0, 4, 8, 12)], [1, 2, 3, 4])
        self.assertEqual([regs[OperatorReg.TL + off] for off in (0, 4, 8, 12)], [0x11, 0x12, 0x13, 0x14])


class Fixes(unittest.TestCase):
    def test_a_splice_reads_the_original_bytes_as_the_replacement(self):
        fm = bytes([0xA0, 0x06, 0x80, 0x80, 0xE6, 0x0C, 0xB0, 0x06, 0xF2])
        fix = RomFix(_SONG + 12, bytes([0x80, 0x80, 0xE6, 0x0C]), b"", "test")
        code = decode_tracks(_memory(_music([fm])), {_SONG + 10: ChannelType.FM}, SONIC1, {fix.address: fix}).code
        self.assertEqual([op.value for op in code.ops if op.kind is OpKind.BYTE], [0xA0, 0x06, 0xB0, 0x06])
        self.assertFalse(any(op.kind is OpKind.EFFECT for op in code.ops))

    def test_a_fix_whose_bytes_differ_is_refused(self):
        with self.assertRaises(RomError):
            apply_fixes(_rom(b"\x01"), (RomFix(_SONG, b"\x02", b"\x03", "test"),))


class SmpsZ80(unittest.TestCase):
    """The Z80 family on a hand-built ROM: a copy loop loads the driver, a bank holds the sounds."""

    _BANK = 0x8000
    _DRIVER = 0x400              # the driver's bytes in the ROM, copied to Z80 $0000
    _FM_TABLE = 0x10             # its FM table's Z80 address

    def _rom(self, music_count: int = 3) -> RomImage:
        rom = bytearray(_HEADER.ljust(2 * self._BANK, b"\0"))

        # lea (z80_ram).l,a6 / lea (driver).l,a5 / move.w #n-1,d0 / move.b (a5)+,(a6)+ / dbra
        driver = bytearray(0x80)
        octaves = b"".join(w.to_bytes(2, "little") for w in FM_FREQUENCIES[:24])      # two octaves
        driver[self._FM_TABLE:self._FM_TABLE + len(octaves)] = octaves
        rom[self._DRIVER:self._DRIVER + len(driver)] = driver
        copy = (bytes.fromhex("4DF900A00000 4BF9".replace(" ", "")) + self._DRIVER.to_bytes(4, "big")
                + bytes.fromhex("303C") + (len(driver) - 1).to_bytes(2, "big") + bytes.fromhex("1CDD51C8FFFC"))
        rom[0x300:0x300 + len(copy)] = copy

        # The sound header at Z80 $8000: +4 the music index, +6 the SFX index; each sound a header
        # and a lone smpsStop, three SFX on FM6
        def z80(at: int) -> bytes:
            return (0x8000 + at).to_bytes(2, "little")

        music_at, sfx_at, sounds_at = 0x10, 0x20, 0x40
        bank = bytearray(0x100)
        bank[4:8] = z80(music_at) + z80(sfx_at)
        at = sounds_at
        for i in range(music_count):
            bank[music_at + 2 * i:music_at + 2 * i + 2] = z80(at)
            bank[at:at + 11] = bytes([0, 0, 1, 0, 1, 0]) + z80(at + 10) + bytes([0, 0, 0xF2])
            at += 11
        for i in range(3):
            bank[sfx_at + 2 * i:sfx_at + 2 * i + 2] = z80(at)
            bank[at:at + 11] = bytes([0, 0, 1, 1, 0x80, 0x06]) + z80(at + 10) + bytes([0, 0, 0xF2])
            at += 11
        rom[self._BANK:self._BANK + len(bank)] = bank
        return RomImage(bytes(rom))

    def test_pointers_are_little_endian_z80_addresses_in_the_bank(self):
        memory = BankedZ80Memory(self._rom(), self._BANK)
        self.assertEqual(memory.word(self._BANK + 4), 0x8010)
        self.assertEqual(memory.header_pointer(self._BANK, self._BANK + 4), self._BANK + 0x10)
        self.assertFalse(memory.contains(self._BANK - 1))
        with self.assertRaisesRegex(RomError, "outside the bank"):
            memory.byte(self._BANK - 1)

    def test_a_fix_reads_in_the_banks_own_memory(self):
        # A data fix's bytes spliced in: little-endian, the bank's pointers unchanged
        patch = BankedZ80Memory(self._rom(), self._BANK).patched(self._BANK + 0x40, bytes([0x12, 0x34]))
        self.assertEqual(patch.word(self._BANK + 0x40), 0x3412)
        self.assertEqual(patch.header_pointer(self._BANK, self._BANK + 4), self._BANK + 0x10)

    def test_the_driver_is_what_the_copy_loop_loads(self):
        self.assertEqual(fm_table(z80_ram(self._rom())), self._FM_TABLE)

    def test_a_copy_past_z80_ram_is_refused(self):
        # The loop's destination moved to $1FF0: its $80 bytes would run past the 8 KB
        rom = bytearray(self._rom().data)
        rom[0x304:0x306] = (0x1FF0).to_bytes(2, "big")
        with self.assertRaisesRegex(RomError, "past its 8 KB"):
            z80_ram(RomImage(bytes(rom)))

    def test_the_bank_and_its_indexes_are_found_by_their_shape(self):
        rom = self._rom()
        index = locate_type0(rom)
        self.assertEqual(sound_bank(rom), self._BANK)
        self.assertEqual(sorted(index.music), [0x81, 0x82, 0x83])
        self.assertEqual(sorted(index.sfx), [0x90, 0x91, 0x92])
        self.assertEqual(index.music[0x81], self._BANK + 0x40)
        self.assertEqual(index.envelopes, ())

    def test_the_music_index_ends_at_the_sfx_index(self):
        # Eight songs would run into the SFX index at +$20: the index stops there
        index = locate_type0(self._rom(music_count=8))
        self.assertEqual(len(index.music), 8)

    def test_a_68k_rom_has_no_z80_driver_to_find(self):
        with self.assertRaisesRegex(RomError, "Z80"):
            sound_bank(_rom(b"\0" * 0x100))


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
        return SongCode(head.header, tracks.code, voices, driver=TYPE0FM.name, dropped=dict(tracks.dropped))

    def test_the_drums_play_on_fm3_and_tempo_0_never_holds(self):
        header = self._code(bytes([0xF2])).header
        song = song_from_code(header, self._code(bytes([0xF2])).code, [])
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
        effects = [(e.effect.flag, e.effect.params) for e in fm1.events if e.effect is not None]
        self.assertEqual(effects, [(CoordFlag.SET_VOICE, [0]), (CoordFlag.PAN, [0x80]), (CoordFlag.SET_VOL, [0x10]),
                                   (CoordFlag.CHANGE_TRANSPOSITION, [12])])
        self.assertEqual([(e.note.note_value, e.note.duration) for e in fm1.events if e.note is not None],
                         [(0xA0, 16), (0xA0, 16)])                       # durations x divider 2
        self.assertEqual(code.dropped, {"$E0 (no handler)": 1})

    def test_notes_play_from_the_songs_own_fm_table(self):
        code = self._code(bytes([0xEF, 0x00, 0xA0, 0x08, 0xF2]))           # $A0: table index $20
        table = tuple(range(0x1000, 0x1000 + 0x60))
        code.fm_frequencies = table
        fm1 = played_song(code.song()).channels["FM1"]
        played = next(p for p in fm1 if not p.rest)
        self.assertEqual(played.note, table[0x20 - 12])                   # transposition -12

    def test_slide_mode_is_refused_and_so_is_the_asm(self):
        with self.assertRaisesRegex(RomError, "slide mode"):
            self._code(bytes([0xFC, 0x01, 0xA0, 0x00, 0x00, 0x08, 0xF2]))
        with self.assertRaisesRegex(ValueError, "no SMPS2ASM spelling"):
            write_asm(self._code(bytes([0xF2])), "Mus81")


class FmDrums(unittest.TestCase):
    """Type 0 FM's drum programs run frame by frame (core/rom/smpsz80/drums.py)."""

    _AT = 0x100
    _TABLE = tuple(0x2400 + i for i in range(0x60))      # block 4, fnum $400 + index

    def _frames(self, program: bytes, modifier: int = NO_TEMPO_HOLDS) -> list[tuple[int, bool, bool]]:
        ram = bytearray(0x2000)
        ram[self._AT:self._AT + len(program)] = program
        player = _Player(Z80RamMemory(RomImage(bytes(ram))), TYPE0FM.flags[ChannelType.FM], self._TABLE,
                         tempo_schedule(modifier)[0], divider=1, transpose=0)
        frames, cut = player.play(self._AT)
        self.assertEqual(cut, "")
        return [(f.word, f.keyed, f.attack) for f in frames]

    # Slide mode: note $B8, slide +5 a frame, a skipped byte, 3 frames; then a tie to $A0 for 2; stop
    _SLIDE_TIE_STOP = bytes([0xFC, 0x01, 0xB8, 0x05, 0x00, 0x03, 0xFC, 0x00, 0xE7, 0xA0, 0x02, 0xF2])

    def test_a_slide_moves_the_word_each_frame_and_a_tie_changes_it_unkeyed(self):
        b8, a0 = self._TABLE[0x38], self._TABLE[0x20]
        self.assertEqual(self._frames(self._SLIDE_TIE_STOP),
                         [(b8, True, True), (b8 + 5, True, False), (b8 + 10, True, False),
                          (a0, True, False), (a0, True, False), (a0, False, False)])

    def test_tempo_holds_stretch_the_program_a_frame_each(self):
        # A hold every 2nd frame (1, 3, 5 ...): a tick every 2 frames, so the 3 + 2 ticks take 10
        # frames and the stop is the 11th
        frames = self._frames(self._SLIDE_TIE_STOP, modifier=2)
        self.assertEqual(len(frames), 11)
        self.assertEqual([f[2] for f in frames].count(True), 1)

    def test_a_slide_wraps_the_octave_as_the_driver_does(self):
        self.assertEqual(_wrap(0x227E), 0x1CFE)      # fnum $27E: down a block, fnum + $280
        self.assertEqual(_wrap(0x24FF), 0x2A7F)      # fnum $4FF: up a block, fnum - $280
        self.assertEqual(_wrap(0x2400), 0x2400)


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
_ASM_DIR = ROOT / "reference" / "smps_drivers" / "sonic_1"


@unittest.skipUnless(_ROM_FILE.exists() and _ASM_DIR.exists(), "needs input/roms/sonic_rev01.bin and reference/smps_drivers/sonic_1/")
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

    def test_the_roms_envelopes_are_the_transcribed_table(self):
        self.assertEqual(read_rom_song(self.rom, 0x81, self.index).psg_envelopes, SONIC1_ENVELOPES)

    def test_the_dac_samples_are_samples_raws(self):
        raws = {"dKick": "kick", "dSnare": "snare", "dTimpani": "timpani"}
        for s in dac_samples(self.rom):
            if s.name in raws:
                self.assertEqual(s.pcm, (ROOT / "samples" / f"{raws[s.name]}.raw").read_bytes(), s.name)
        self.assertEqual([s.pitch for s in dac_samples(self.rom)], [23, 1, 27, 0x12, 0x15, 0x1C, 0x1D])


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual(len(song.psg_envelopes), 6)
        self.assertNotEqual(song.psg_envelopes["fTone_03"], SONIC1_ENVELOPES["fTone_03"])
        self.assertEqual(len(song.psg_envelopes["fTone_06"].steps), 16 + 41)     # runs on into envelope 5
        dac = {e.note.dac_name for e in song.channels[0].events if e.note and not e.note.is_rest}
        self.assertTrue(dac and all(name.startswith("dac") for name in dac))

    def test_the_dac_samples(self):
        samples = {s.sound: s for s in dac_samples(self.rom)}
        self.assertEqual([len(samples[b].pcm) for b in range(0x81, 0x86)], [1280, 4096, 1536, 4266, 3584])
        self.assertEqual((samples[0x8C].of, samples[0x8C].pitch, samples[0x90].pitch), (0x85, 0x16, 0x1E))
        self.assertEqual({b: round(samples[b].rate) for b in (0x81, 0x82, 0x84)},
                         {0x81: 10739, 0x82: 6770, 0x84: 15193})     # the rips: 10765, ~6770, ~15190


_GOLDEN_AXE = ROOT / "input" / "roms" / "Golden Axe (World) (Rev A).md"


@unittest.skipUnless(_GOLDEN_AXE.exists(), "needs input/roms/Golden Axe (World) (Rev A).md")
class GoldenAxe(unittest.TestCase):
    """SMPS Z80 Type 0 FM, no disassembly: what docs/todo/binary_import.md's Phase 3 probe found."""

    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(_GOLDEN_AXE)

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
        self.assertEqual(songs[0x81].fm_frequencies[1:3], (0x283, 0x2A4))             # nC0: Z80 $07D9
        self.assertEqual(len(songs[0x81].fm_frequencies), len(FM_FREQUENCIES))

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
