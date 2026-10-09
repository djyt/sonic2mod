"""Streets of Rage's driver (SMPS 68k with MUCOM-style track code): the grammar on hand-built
bytes, then the ROM when it is present (docs/todo/streets_of_rage.md).

    python -m pytest tests/test_rom_mucom.py -q
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from roms import STREETS_OF_RAGE_ROM, needs_streets_of_rage

from core.drivers import detect_variant, locate_sounds, read_rom_code, read_rom_song
from core.drivers.reference import PSG_FREQUENCIES
from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.mucom import MUCOM
from core.rom import RomImage
from core.rom.grammar import track_label
from core.rom.image import RomError
from core.rom.tracks import decode_tracks
from core.smps import (
    MAX_PSG,
    REST,
    ChannelType,
    CoordFlag,
    SmpsChannelHeader,
    SmpsSongHeader,
    song_from_code,
    source_map,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_AT = 0x200                     # where a hand-built track starts
# The ROM's first FM volume steps (the rest is read from it)
_RULES = replace(MUCOM.rules, volume_steps={**MUCOM.rules.volume_steps, ChannelType.FM: {-1: 0x2D, 0: 0x36, 1: 0x33, 2: 0x30}})


def _walk(track: bytes, kind: ChannelType = ChannelType.FM, volume: int = 0):
    """A track's bytes, decoded and walked as one channel (its header volume `volume`)."""
    memory = Relative68kMemory(RomImage(_HEADER + track))
    code = decode_tracks(memory, {_AT: kind}, MUCOM).code
    header = SmpsSongHeader(channels=[SmpsChannelHeader(channel_type=kind, label=track_label(_AT), volume=volume)])
    return song_from_code(header, code, [], _RULES).channels[0]


def _notes(channel) -> list[tuple]:
    """(tick, note, duration, tied) of each note and rest."""
    return [(e.tick_position, e.note.note_value, e.note.duration, e.note.is_no_attack)
            for e in channel.events if e.note is not None]


def _effects(channel, flag: CoordFlag) -> list[list]:
    return [list(e.effect.values) for e in channel.events if e.effect is not None and e.effect.flag == flag]


class Grammar(unittest.TestCase):
    def test_duration_then_note_rest_and_end(self):
        # A4 ($49: octave 4, semitone 9) for 6, a rest of 3, B7 ($7B) for 2, the end
        ch = _walk(bytes([0x06, 0x49, 0x83, 0x02, 0x7B, 0x00]))
        self.assertEqual(_notes(ch), [(0, 0x81 + 57, 6, False), (6, REST, 3, False), (9, 0xE0, 2, False)])

    def test_psg_rows_play_an_octave_up_and_clamp(self):
        # Row 3 = C4 (Sonic 1's PSG entry 12); rows 0-1 read row 2 (C3), rows 7-9 row 6
        ch = _walk(bytes([1, 0x30, 1, 0x05, 1, 0x95, 0x00]), ChannelType.PSG)
        self.assertEqual([n for _, n, _, _ in _notes(ch)], [0x81 + 12, 0x81 + 5, 0x81 + 48 + 5])

    def test_a_note_past_the_octave_table_is_refused(self):
        with self.assertRaisesRegex(RomError, "semitone 12"):
            _walk(bytes([1, 0x4C, 0x00]))

    def test_a_tie_holds_into_the_next_note(self):
        ch = _walk(bytes([4, 0x40, 0xFD, 4, 0x42, 0x00]))
        self.assertEqual([tied for *_, tied in _notes(ch)], [False, True])

    def test_a_loop_with_a_break_skips_the_tail_on_its_last_pass(self):
        # [ C4 / D4 ]3: C D C D C, then E4.  $F5 points at $F6's count; $F6 jumps back to the body;
        # $FE jumps past $F6 (offset from its word's end + 2)
        track = bytes([0xF5, 0x0B, 0x00,          # $200: the count at $201 + 11 = $20C
                       1, 0x40,                   # $203 C4
                       0xFE, 0x05, 0x00,          # $205: past the loop: $205 + 5 + 5 = $20F
                       1, 0x42,                   # $208 D4
                       0xF6, 0x01, 0x03, 0x0A, 0x00,   # $20A: 3 passes, back to $20D - 10 = $203
                       1, 0x44, 0x00])            # $20F E4
        ch = _walk(track)
        self.assertEqual([n - 0x81 for _, n, _, _ in _notes(ch)], [48, 50, 48, 50, 48, 52])

    def test_a_break_drops_a_tie(self):
        # [ C4 & / ]2 then C4: the break on the second pass leaves the tie behind
        track = bytes([0xF5, 0x0A, 0x00, 1, 0x40, 0xFD, 0xFE, 0x03, 0x00,
                       0xF6, 0x01, 0x02, 0x09, 0x00, 1, 0x40, 0x00])
        self.assertEqual([tied for *_, tied in _notes(_walk(track))], [False, True, False])

    def test_the_drum_track_plays_the_sample_f0_chose(self):
        ch = _walk(bytes([2, 0x00, 0xF0, 0x02, 2, 0x00, 0xF0, 0x04, 2, 0x01, 0x00]), ChannelType.DAC)
        self.assertEqual([(n, e.note.dac_name) for (_, n, _, _), e in
                          zip(_notes(ch), [e for e in ch.events if e.note], strict=True)],
                         [(REST, ""), (0x82, "dac82"), (0x84, "dac84")])

    def test_flags_spelled_in_smps_terms(self):
        # pan right, vibrato (delay 1, speed 2, depth -40, count 4), detune +195 (set)
        ch = _walk(bytes([0xF8, 0x02, 0xF4, 0x00, 0x01, 0x02, 0xD8, 0xFF, 0x04, 0xF2, 0xC3, 0x00, 0x00, 0x00]))
        self.assertEqual(_effects(ch, CoordFlag.PAN), [[0x40]])
        self.assertEqual(_effects(ch, CoordFlag.MOD_SET), [[1, 2, -40, 5]])
        self.assertEqual(_effects(ch, CoordFlag.DETUNE), [[195]])

    def test_the_psg_detunes_by_the_word_over_16(self):
        ch = _walk(bytes([0xF2, 0xF0, 0xFF, 0x00, 0x00]), ChannelType.PSG)
        self.assertEqual(_effects(ch, CoordFlag.DETUNE), [[-1]])

    def test_a_detune_that_adds_adds_to_the_word(self):
        # FM: set 20, add 30.  PSG: add 100, add -100: the word's sum over 16 (6, then 0; not -1)
        fm = _walk(bytes([0xF2, 0x14, 0x00, 0x00, 0xF2, 0x1E, 0x00, 0x01, 0x00]))
        self.assertEqual(_effects(fm, CoordFlag.DETUNE), [[20], [50]])
        psg = _walk(bytes([0xF2, 0x64, 0x00, 0x01, 0xF2, 0x9C, 0xFF, 0x01, 0x00]), ChannelType.PSG)
        self.assertEqual(_effects(psg, CoordFlag.DETUNE), [[6], [0]])

    def test_fm_volume_is_a_step_of_the_table_plus_the_header_volume(self):
        # $F1 2, $FB -3 (to step -1: the bytes before the table), $FB +2
        ch = _walk(bytes([0xF1, 0x02, 0xFB, 0xFD, 0xFB, 0x02, 0x00]), volume=4)
        self.assertEqual(_effects(ch, CoordFlag.SET_VOL), [[0x30 + 4], [0x2D + 4], [0x33 + 4]])

    def test_psg_volume_sets_minus_v_and_steps_the_attenuation(self):
        # $F1 3: att (-3 & 15) + header volume -2 = 11; $FB 2 takes 2 from it; $F1 0: 0 - 2, below 0
        ch = _walk(bytes([0xF1, 0x03, 0xFB, 0x02, 0xF1, 0x00, 0x00]), ChannelType.PSG, volume=0xFE)
        self.assertEqual(_effects(ch, CoordFlag.SET_VOL), [[11], [9], [-2]])

    def test_noise_keeps_tone_3_where_the_last_tone_note_left_it(self):
        # A tone note (C4), noise, a noise note: tone 3 still holds C4.  None before: nMaxPSG
        ch = _walk(bytes([1, 0x30, 0xF7, 0x01, 1, 0x55, 0x00]), ChannelType.PSG)
        self.assertEqual([n for _, n, _, _ in _notes(ch)], [0x81 + 12, 0x81 + 12])
        ch = _walk(bytes([0xF7, 0x01, 1, 0x55, 0x00]), ChannelType.PSG)
        self.assertEqual([n for _, n, _, _ in _notes(ch)], [MAX_PSG])

    def test_the_gate_cuts_a_note_short_but_the_ones_the_driver_spares(self):
        # Gate 2: C4 6 (cut: 4 and a rest of 2), D4 6 then a tie (spared), D4 6 tied (FM spares
        # it, the PSG cuts it), C4 1 (no longer than the gate)
        track = bytes([0xF3, 0x02, 6, 0x40, 6, 0x42, 0xFD, 6, 0x42, 1, 0x40, 0x00])
        self.assertEqual([(t, d) for t, _, d, _ in _notes(_walk(track))], [(0, 4), (4, 2), (6, 6), (12, 6), (18, 1)])
        self.assertEqual([(t, d) for t, _, d, _ in _notes(_walk(track, ChannelType.PSG))],
                         [(0, 4), (4, 2), (6, 6), (12, 4), (16, 2), (18, 1)])

    def test_a_rest_after_a_tie_keys_fm_off_a_frame_in_the_psg_at_once(self):
        track = bytes([4, 0x40, 0xFD, 0x85, 0x00])
        self.assertEqual([(t, n, d, tied) for t, n, d, tied in _notes(_walk(track))][1:],
                         [(4, REST, 1, True), (5, REST, 4, False)])
        self.assertEqual(_notes(_walk(track, ChannelType.PSG))[1:], [(4, REST, 5, False)])

    def test_the_jump_back_drops_an_fm_tie_the_psg_keeps(self):
        # C4 & / loop: D4 & / jump back.  The first pass ties D4; a replay's D4 attacks on FM (the
        # jump cleared the tie), stays tied on the PSG
        track = bytes([4, 0x40, 0xFD, 4, 0x42, 0xFD, 0xFF, 0xFF, 0xFB])      # $FF: its address + 2 + the word
        self.assertIs(_walk(track).replay_tie, False)
        self.assertIs(_walk(track, ChannelType.PSG).replay_tie, True)
        # Without the tie before the jump, the PSG's replay attacks too: its first pass took the
        # tie from before the label
        track = bytes([4, 0x40, 0xFD, 4, 0x42, 0xFF, 0xFF, 0xFC])
        self.assertIs(_walk(track, ChannelType.PSG).replay_tie, False)

    def test_f7_is_fm3_special_mode_on_fm_and_noise_on_the_psg(self):
        fm = decode_tracks(Relative68kMemory(RomImage(_HEADER + bytes([0xF7, 0x64, 0, 0, 0, 0x00]))),
                           {_AT: ChannelType.FM}, MUCOM)
        self.assertEqual(fm.dropped, {"FM3 special mode": 1})
        self.assertEqual(_effects(_walk(bytes([0xF7, 0x01, 0x00]), ChannelType.PSG), CoordFlag.PSG_FORM), [[0xE7]])


@needs_streets_of_rage
class StreetsOfRage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(STREETS_OF_RAGE_ROM)
        cls.index = locate_sounds(cls.rom)

    def test_pinned_and_located_by_the_code_that_reads_each_table(self):
        self.assertIs(detect_variant(self.rom), MUCOM)
        self.assertEqual((min(self.index.music), max(self.index.music), self.index.music[0x81]), (0x81, 0x91, 0x74C6E))
        self.assertEqual(self.index.music[0x8D], self.index.music[0x8E])          # Level Clear twice
        self.assertEqual((len(self.index.sfx), min(self.index.sfx)), (48, 0xA0))
        self.assertEqual(len(self.index.envelopes), 5)

    def test_fm_octave_volume_steps_and_psg_rows(self):
        rules = read_rom_song(self.rom, 0x81, self.index).rules
        self.assertEqual(len(rules.fm_frequencies), 97)
        self.assertEqual(rules.fm_frequencies[1 + 57], 4 << 11 | 0x43C)          # A4: block 4
        steps = rules.volume_steps[ChannelType.FM]
        self.assertEqual([steps[s] for s in (-4, -1, 0, 1, 19, 20)], [0x36, 0x2D, 0x36, 0x33, 0x02, 0x00])
        self.assertEqual(PSG_FREQUENCIES[0], 0x356)

    def test_voices_carry_no_carrier_tl(self):
        # The driver writes the carriers' TL from the volume as it loads a voice
        song = read_rom_song(self.rom, 0x81, self.index)
        self.assertTrue(all(voice.registers()[r] == 0 for voice in song.voices for r in voice.carrier_registers))

    def test_a_register_write_plays_as_a_patched_voice(self):
        # Beatnik on the Ship, FM1: $FA $6C $0F, $7C $11, $68 $0E, $78 $0F after setting voice 2
        song = read_rom_song(self.rom, 0x85, self.index)
        fm1 = source_map(song)["FM1"]
        sets = [e.effect.index for e in fm1.events if e.effect is not None and e.effect.flag == CoordFlag.SET_VOICE]
        voices = {v.index: v for v in song.voices}
        patched = voices[sets[4]].registers()
        self.assertEqual([patched[r] for r in (0x6C, 0x7C, 0x68, 0x78)], [0x0F, 0x11, 0x0E, 0x0F])
        self.assertEqual(sets[:5], [sets[0], *range(sets[1], sets[1] + 4)])     # each write a new copy
        with self.assertRaisesRegex(ValueError, "carrier's TL"):
            voices[sets[0]].patched(voices[sets[0]].carrier_registers[0], 0x10)

    def test_a_jump_back_after_a_tie_attacks_on_the_replay(self):
        # You Became the Bad Guy!: FM1, FM4 and FM5 tie into their loop's first note on the first pass
        tracks = source_map(read_rom_song(self.rom, 0x8F, self.index))
        self.assertEqual([tracks[n].replay_tie for n in ("FM1", "FM4", "FM5")], [False, False, False])

    def test_every_song_reads_on_its_chip_channels(self):
        for sid in self.index.music:
            song = read_rom_song(self.rom, sid, self.index)
            self.assertEqual(list(source_map(song))[:6], ["FM1", "FM2", "FM3", "FM4", "FM5", "DAC"], f"${sid:02X}")
        self.assertEqual(list(source_map(read_rom_song(self.rom, 0x83, self.index)))[6:], ["PSG3", "PSG2", "PSG1"])

    def test_what_is_read_and_left_out(self):
        dropped = Counter()
        for sid in self.index.music:
            dropped.update(read_rom_code(self.rom, sid, self.index).dropped)
        self.assertEqual(set(dropped), {"timer write", "LFO", "FM3 special mode", "$F0 (no PSG effect)",
                                        "$F8 (no PSG effect)", "$F1 (no DAC effect)", "$FB (no DAC effect)"})

    def test_envelope_3_ends_in_silence(self):
        song = read_rom_song(self.rom, 0x81, self.index)
        self.assertEqual(song.rules.psg_envelopes["fTone_03"].steps, (0, 0, 2, 3, 4, 5, 15))

    def test_sfx_are_listed_not_read(self):
        with self.assertRaisesRegex(RomError, "music only"):
            read_rom_code(self.rom, 0xA0, self.index)

    def test_a_loop_end_without_its_start_plays_as_written(self):
        # Stealthy Steps' noise track: $F6 at $7E196 closes a loop no $F5 opened; 16 passes
        noise = source_map(read_rom_song(self.rom, 0x89, self.index))["PSG3"]
        self.assertEqual(noise.loop_tick, 0)
        self.assertEqual(sum(1 for e in noise.events if e.note and e.note.duration == 90), 16)


if __name__ == "__main__":
    unittest.main()
