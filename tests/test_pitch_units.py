"""Real-pitch names and intervals (core/audio/pitch.py), what the VGM tools print; the fnum a
sample renders at (ym2612/renderer.py).

    python -m pytest tests -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio import cents, hz_to_midi, midi_name, pitch_name
from core.chips import freq_word_hz
from core.drivers.reference import FM_FREQUENCIES
from core.smps import FmFrame, SmpsVoice, VoiceField
from ym2612.renderer import note_to_fnum_block, render_frames, render_layers
from ym2612.wrapper import output_rate


class Names(unittest.TestCase):
    def test_a_frequency_is_named_by_its_nearest_semitone(self):
        self.assertEqual(pitch_name(440.0), "A4")
        self.assertEqual(pitch_name(261.63), "C4")
        self.assertEqual(pitch_name(277.18 * 1.01), "C#4")      # 17 cents sharp still rounds to C#
        self.assertEqual(pitch_name(16.35), "C0")

    def test_no_frequency_has_no_name(self):
        self.assertEqual(pitch_name(0.0), "---")

    def test_midi_numbers(self):
        self.assertEqual(midi_name(69), "A4")
        self.assertEqual(midi_name(61), "C#4")
        self.assertEqual(midi_name(0), "C-1")
        self.assertAlmostEqual(hz_to_midi(880.0), 81.0)


class Intervals(unittest.TestCase):
    def test_cents(self):
        self.assertAlmostEqual(cents(880.0, 440.0), 1200.0)
        self.assertAlmostEqual(cents(440.0, 880.0), -1200.0)

    def test_cents_of_nothing_is_nan(self):
        self.assertTrue(math.isnan(cents(0.0, 440.0)))
        self.assertTrue(math.isnan(cents(440.0, 0.0)))



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


def _hz(mono, rate: int) -> float:
    """A sine's frequency by its rising zero crossings (the attack's first tenth skipped)."""
    start = len(mono) // 10
    rising = [i for i in range(start + 1, len(mono)) if mono[i - 1] < 0 <= mono[i]]
    return (len(rising) - 1) * rate / (rising[-1] - rising[0])



class RenderedFrames(unittest.TestCase):
    """An FM drum program rendered frame by frame (ym2612/renderer.py render_frames)."""

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


if __name__ == "__main__":
    unittest.main()
