"""The fnum a sample renders at and the frames it renders (core/synth/fm_render.py).

    python -m pytest tests/core/synth/test_fm_render.py -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.chips import FmLfo, freq_word_hz
from core.chips.ym2612 import output_rate
from core.drivers.reference import FM_FREQUENCIES
from core.smps import FmFrame, SmpsVoice, VoiceField
from core.synth.fm_render import note_to_fnum_block, render_frames, render_layers


def _hz(mono, rate: int) -> float:
    """A sine's frequency by its rising zero crossings (the attack's first tenth skipped)."""
    start = len(mono) // 10
    rising = [i for i in range(start + 1, len(mono)) if mono[i - 1] < 0 <= mono[i]]
    return (len(rising) - 1) * rate / (rising[-1] - rising[0])


class RenderedPitch(unittest.TestCase):
    def test_a_sample_renders_at_its_songs_table_word(self):
        self.assertEqual(note_to_fnum_block(0, fm_frequencies=FM_FREQUENCIES), (FM_FREQUENCIES[13] & 0x7FF, FM_FREQUENCIES[13] >> 11))   # C1
        golden_axe_c1 = 0xA7E                                       # 15.6 cents under Sonic 1's
        table = (*FM_FREQUENCIES[:13], golden_axe_c1, *FM_FREQUENCIES[14:])
        self.assertEqual(note_to_fnum_block(0, fm_frequencies=table), (0x27E, 1))

    def test_a_voice_in_special_mode_plays_each_operator_at_its_offset(self):
        # Only OP4 sounds (algorithm 7, the others at TL 127): +100 FNUM on it is its pitch,
        # also as the second layer of a composite (it moves to channel 3)
        voice = SmpsVoice(0, algorithm=7, operators={VoiceField.ATTACK_RATE: (31, 31, 31, 31),
                                                     VoiceField.MULTIPLE: (1, 1, 1, 1),
                                                     VoiceField.TOTAL_LEVEL: (0, 127, 127, 127)})
        special = SmpsVoice(1, algorithm=7, operators=voice.operators, fnum_offsets=(100, 0, 0, 0))
        silent = SmpsVoice(2, algorithm=7, operators={VoiceField.TOTAL_LEVEL: (127, 127, 127, 127)})
        word = FM_FREQUENCIES[13 + 33]                                  # A3
        for layers, offset in (([(voice, 0, 0, 0)], 0), ([(special, 0, 0, 0)], 100),
                               ([(silent, 0, 0, 0), (special, 0, 0, 0)], 100)):
            mono, rate = render_layers(layers, 33, sustain_secs=0.5, release_secs=0.0, fm_frequencies=FM_FREQUENCIES)
            self.assertAlmostEqual(_hz(mono, rate) / freq_word_hz(word + offset), 1.0, delta=0.005)


    def test_a_pitch_envelope_steps_the_word_a_frame_at_a_time(self):
        # 30 frames at the note's word, then 30 at +200 FNUM: each half sounds its pitch
        voice = SmpsVoice(0, algorithm=7, operators={VoiceField.ATTACK_RATE: (31, 31, 31, 31),
                                                     VoiceField.MULTIPLE: (1, 1, 1, 1),
                                                     VoiceField.TOTAL_LEVEL: (0, 127, 127, 127)})
        word = FM_FREQUENCIES[13 + 33]                                  # A3
        mono, rate = render_layers([(voice, 0, 0, 0)], 33, sustain_secs=1.0, release_secs=0.0, fm_frequencies=FM_FREQUENCIES,
                                   envelopes=[(0,) * 30 + (200,) * 30], frame_hz=60.0)
        half = len(mono) // 2
        self.assertAlmostEqual(_hz(mono[:half], rate) / freq_word_hz(word), 1.0, delta=0.005)
        self.assertAlmostEqual(_hz(mono[half:], rate) / freq_word_hz(word + 200), 1.0, delta=0.005)

    def test_a_voice_under_the_lfo_plays_its_tremolo(self):
        # One carrier with AM on, AMS 3 (11.8 dB) at LFO frequency 6 (46 Hz): its level swings;
        # without the LFO it holds (A5: a window of 2.5 ms holds two cycles)
        operators = {VoiceField.ATTACK_RATE: (31, 31, 31, 31), VoiceField.MULTIPLE: (1, 1, 1, 1),
                     VoiceField.AMP_MOD: (1, 1, 1, 1), VoiceField.TOTAL_LEVEL: (0, 127, 127, 127)}
        swings = []
        for lfo in (None, FmLfo(6, 0, 3)):
            voice = SmpsVoice(0, algorithm=7, operators=operators, lfo=lfo)
            mono, rate = render_layers([(voice, 0, 0, 0)], 57, sustain_secs=0.5, release_secs=0.0,
                                       fm_frequencies=FM_FREQUENCIES)
            window = rate // 400
            peaks = [max(abs(v) for v in mono[i:i + window]) for i in range(len(mono) // 2, len(mono) - window, window)]
            swings.append(20 * math.log10(max(peaks) / min(peaks)))
        self.assertLess(swings[0], 1.0)
        self.assertGreater(swings[1], 6.0)


class RenderedFrames(unittest.TestCase):
    """An FM drum program rendered frame by frame (core/synth/fm_render.py render_frames)."""

    _VOICE = SmpsVoice(0, algorithm=7, operators={VoiceField.ATTACK_RATE: (31, 31, 31, 31),
                                                  VoiceField.MULTIPLE: (1, 1, 1, 1),
                                                  VoiceField.RELEASE_RATE: (15, 15, 15, 15)})

    def test_each_frame_lasts_a_frame_and_the_key_off_silences(self):
        word = FM_FREQUENCIES[13 + 33]                                  # A3
        frames = [FmFrame(word, True, True), FmFrame(word, True), FmFrame(word, False)]
        mono, rate = render_frames(self._VOICE, 0, frames, 60.0, tail_secs=0.05)
        self.assertEqual(rate, output_rate(7670454))
        self.assertEqual(len(mono), round(3 * rate / 60.0) + math.ceil(0.05 * rate))
        held = max(abs(x) for x in mono[:round(2 * rate / 60.0)])
        tail = max(abs(x) for x in mono[-100:])
        self.assertGreater(held, 0)
        self.assertLess(tail, held / 100)                               # released: nothing left

    def test_special_mode_plays_each_operator_at_its_word_keyed_by_its_mask(self):
        # Algorithm 7, OP4 alone audible: its word is FM3's own (slot +C); keyed with OP1 / OP2
        # only (Space Harrier II's unit A) it is silent, with OP4 in the mask it sounds
        voice = SmpsVoice(0, algorithm=7, operators={**self._VOICE.operators, VoiceField.TOTAL_LEVEL: (0, 127, 127, 127)})
        word = FM_FREQUENCIES[13 + 33]                                  # A3
        slots = (0, 0, 0, word)                                         # OP1 OP3 OP2 OP4
        for keys, sounds in ((0b0011, False), (0b1000, True)):
            frames = [FmFrame(word, True, True, slots, keys)] + [FmFrame(word, True, False, slots, keys)] * 60
            mono, rate = render_frames(voice, 0, frames, 60.0)
            self.assertEqual(max(abs(x) for x in mono) > 0, sounds)
            if sounds:
                self.assertAlmostEqual(_hz(mono, rate) / freq_word_hz(word), 1.0, delta=0.005)


if __name__ == "__main__":
    unittest.main()
