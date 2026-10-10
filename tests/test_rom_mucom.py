"""Streets of Rage's driver (SMPS 68k with MUCOM-style track code): the grammar on hand-built
bytes, then the ROM when it is present (docs/smps_variants.md).

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

from roms import STREETS_OF_RAGE_RIPS, STREETS_OF_RAGE_ROM, needs_streets_of_rage, needs_streets_of_rage_rips

from core.chips import FmLfo
from core.drivers import detect_variant, locate_sounds, read_rom_code, read_rom_song
from core.drivers.reference import PSG_FREQUENCIES
from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.mucom import MUCOM
from core.drivers.smps68k.mucom.dac import _RunDpcm
from core.rom import RomImage
from core.rom.grammar import track_label
from core.rom.image import RomError
from core.rom.tracks import decode_tracks
from core.smps import (
    MAX_PSG,
    REST,
    ChannelType,
    CoordFlag,
    SetVoice,
    SmpsChannelHeader,
    SmpsSongHeader,
    SmpsVoice,
    song_from_code,
    source_map,
)
from core.source import read_dac
from core.vgm import read_vgm

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_AT = 0x200                     # where a hand-built track starts
# The ROM's first FM volume steps (the rest is read from it)
_FM = replace(MUCOM.rules.track(ChannelType.FM), volume_steps={-1: 0x2D, 0: 0x36, 1: 0x33, 2: 0x30})
_RULES = replace(MUCOM.rules, tracks={**MUCOM.rules.tracks, ChannelType.FM: _FM})


def _song(track: bytes, kind: ChannelType = ChannelType.FM, volume: int = 0, chip_channel: str = "",
          voices: tuple[SmpsVoice, ...] = ()):
    """A track's bytes, decoded and walked as a song's one channel (its header volume `volume`,
    on `chip_channel`)."""
    memory = Relative68kMemory(RomImage(_HEADER + track))
    code = decode_tracks(memory, {_AT: kind}, MUCOM).code
    header = SmpsSongHeader(channels=[SmpsChannelHeader(channel_type=kind, label=track_label(_AT), volume=volume,
                                                        chip_channel=chip_channel)])
    return song_from_code(header, code, list(voices), _RULES)


def _tracks(*tracks: bytes, voices: tuple[SmpsVoice, ...] = ()):
    """FM tracks' bytes, one after the other, walked as a song's FM1, FM2 ..."""
    at, starts, image = _AT, [], b""
    for track in tracks:
        starts.append(at)
        image += track
        at += len(track)
    code = decode_tracks(Relative68kMemory(RomImage(_HEADER + image)), dict.fromkeys(starts, ChannelType.FM), MUCOM).code
    header = SmpsSongHeader(channels=[SmpsChannelHeader(channel_type=ChannelType.FM, label=track_label(a))
                                      for a in starts])
    return song_from_code(header, code, list(voices), _RULES)


def _walk(track: bytes, kind: ChannelType = ChannelType.FM, volume: int = 0):
    """A track's bytes, decoded and walked as one channel (its header volume `volume`)."""
    return _song(track, kind, volume).channels[0]


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

    def test_f7_is_noise_on_the_psg(self):
        self.assertEqual(_effects(_walk(bytes([0xF7, 0x01, 0x00]), ChannelType.PSG), CoordFlag.PSG_FORM), [[0xE7]])


class HardwareLfo(unittest.TestCase):
    """`$FC f p a`: the chip's LFO frequency (every track's), this track's FMS and AMS; each note's
    voice a copy under the LFO it plays with (core/smps/lfo.py)."""

    _VOICES = (SmpsVoice(0), SmpsVoice(1))

    def _lfos(self, song, channel: int) -> list[tuple[int, FmLfo | None]]:
        """(tick, LFO) of each note the channel attacks."""
        voices = {v.index: v for v in song.voices}
        voice, out = None, []
        for ev in song.channels[channel].events:
            if isinstance(ev.effect, SetVoice):
                voice = voices[ev.effect.index]
            if ev.note is not None and not ev.note.is_rest:
                out.append((ev.tick_position, voice.lfo if voice is not None else None))
        return out

    def test_a_note_plays_at_the_last_frequency_any_track_wrote(self):
        # FM1: voice 0, LFO 4 (FMS 6), C4 at 0 and at 10.  FM2: rest 5, voice 1, LFO 2 (FMS 3, AMS 2), C4
        song = _tracks(bytes([0xF0, 0x00, 0xFC, 0x04, 0x06, 0x00, 10, 0x40, 10, 0x40, 0x00]),
                       bytes([0x85, 0xF0, 0x01, 0xFC, 0x02, 0x03, 0x02, 10, 0x40, 0x00]), voices=self._VOICES)
        self.assertEqual(self._lfos(song, 0), [(0, FmLfo(4, 6, 0)), (10, FmLfo(2, 6, 0))])
        self.assertEqual(self._lfos(song, 1), [(5, FmLfo(2, 3, 2))])

    def test_no_sensitivity_plays_no_lfo_and_a_later_track_writes_after_the_note(self):
        # FM1: LFO 4 with nothing to move, C4.  FM2 writes LFO 7 on the same frame: after FM1's read
        song = _tracks(bytes([0xF0, 0x00, 0xFC, 0x04, 0x00, 0x00, 10, 0x40, 0x00]),
                       bytes([0xF0, 0x01, 0xFC, 0x07, 0x01, 0x00, 10, 0x40, 0x00]), voices=self._VOICES)
        self.assertEqual(self._lfos(song, 0), [(0, None)])
        song = _tracks(bytes([0xF0, 0x00, 0xFC, 0x04, 0x02, 0x00, 10, 0x40, 0x00]),
                       bytes([0xF0, 0x01, 0xFC, 0x07, 0x01, 0x00, 10, 0x40, 0x00]), voices=self._VOICES)
        self.assertEqual(self._lfos(song, 0), [(0, FmLfo(4, 2, 0))])


class SpecialMode(unittest.TestCase):
    """`$F7 a b c d` on FM3: each operator at the note's word plus its offset (OP4 first), as a
    copy of the voice (core/smps/voice_patch.py)."""

    _VOICES = (SmpsVoice(0, algorithm=3), SmpsVoice(1, algorithm=4))

    def _voices_set(self, track: bytes, chip_channel: str = "FM3") -> list[SmpsVoice]:
        song = _song(track, chip_channel=chip_channel, voices=self._VOICES)
        voices = {v.index: v for v in song.voices}
        return [voices[i] for [i] in _effects(song.channels[0], CoordFlag.SET_VOICE)]

    def test_the_offsets_play_from_the_voice_set_and_across_the_next(self):
        # voice 0, $F7 100 0 0 0, C4, voice 1, C4, $F7 0 0 0 0 (all 0: normal mode), C4
        track = bytes([0xF0, 0x00, 0xF7, 0x64, 0, 0, 0, 1, 0x40, 0xF0, 0x01, 1, 0x40, 0xF7, 0, 0, 0, 0, 1, 0x40, 0x00])
        voices = self._voices_set(track)
        self.assertEqual([v.fnum_offsets for v in voices], [None, (100, 0, 0, 0), (100, 0, 0, 0), None])
        self.assertEqual([v.algorithm for v in voices], [3, 3, 4, 4])
        self.assertEqual([v.channel_fnum_offset for v in voices], [0, 100, 100, 0])

    def test_one_copy_per_voice_and_offsets(self):
        track = bytes([0xF0, 0x00, 0xF7, 0x64, 0, 0, 0, 1, 0x40, 0xF0, 0x00, 1, 0x40, 0x00])
        first, second = self._voices_set(track)[1:]
        self.assertIs(first, second)

    def test_a_voice_and_its_special_mode_render_apart(self):
        # The render cache keys a copy on its offsets: the base voice's render is not the copy's
        from ym2612.sample_generator import _voice_key
        plain, copy = self._voices_set(bytes([0xF0, 0x00, 0xF7, 0x64, 0, 0, 0, 1, 0x40, 0x00]))
        self.assertNotEqual(_voice_key(plain), _voice_key(copy))

    def test_only_channel_3_has_the_mode(self):
        with self.assertRaisesRegex(ValueError, "special mode on FM1"):
            self._voices_set(bytes([0xF7, 0x64, 0, 0, 0, 0x00]), "FM1")


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
        steps = rules.track(ChannelType.FM).volume_steps
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
        self.assertEqual(set(dropped), {"timer write", "$F0 (no PSG effect)",
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


class Samples(unittest.TestCase):
    # A delta table (byte 0: a run's length, 2) and its nibbles: +1 (n 2), a run (0), -2 (n 15)
    _DELTAS = bytes([2, 0, 1, 2, 4, 8, 16, 32, 64, 128, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF, 0xFE])

    def test_a_nibble_adds_its_delta_and_0_runs_the_last(self):
        pcm = _RunDpcm().decode(self._DELTAS + bytes([0x20, 0xF0]))
        self.assertEqual([b ^ 0x80 for b in pcm], [0x81, 0x82, 0x83, 0x81, 0x7F, 0x7D])

    def test_a_sample_that_starts_with_a_run_is_refused(self):
        with self.assertRaisesRegex(RomError, "starts with a run"):
            _RunDpcm().decode(self._DELTAS + bytes([0x02]))

    def test_cycles_count_each_path(self):
        # Pitch 1, the stall out: a high and a low nibble average 216 + 13
        literal = _RunDpcm().cycles(self._DELTAS + bytes([0x22]), 1) * (1 - 0.016)
        self.assertAlmostEqual(literal, 229)


@needs_streets_of_rage_rips
class SamplesAgainstTheRips(unittest.TestCase):
    def test_each_music_sample_is_in_a_rips_bank(self):
        samples = read_dac(STREETS_OF_RAGE_ROM)
        self.assertEqual([s.sound for s in samples], [0x81, 0x82, 0x83, 0x84])
        banks = [read_vgm(p).pcm for p in sorted(STREETS_OF_RAGE_RIPS.glob("*.vgz"))]
        for s in samples:
            unsigned = bytes(b ^ 0x80 for b in s.pcm)
            self.assertTrue(any(unsigned in bank for bank in banks), f"${s.sound:02X}")
        self.assertEqual([round(s.rate) for s in samples], [12983, 15381, 9464, 15381])


if __name__ == "__main__":
    unittest.main()
