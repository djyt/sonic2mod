"""The walk's state and MOD routing (core/plan/driver_state.py): a PSG track's instrument as its
envelope and noise form change, noise turned off again (Streets of Rage 2's `$F7 8`); a PSG note
detuned past half a semitone routing as the note it sounds.

    python -m pytest tests/core/plan/test_driver_state.py -q
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.drivers.reference import SONIC1_RULES
from core.plan.driver_state import DriverState, resolve_note
from core.smps import Detune, PsgForm, PsgTable, PsgVoice

_TONE_1, _TONE_2, _NOISE = 5, 6, 9           # the MOD instruments the stub config routes to
_FORM = 0xE4


def _entry(instrument: int) -> SimpleNamespace:
    return SimpleNamespace(mod_instrument=instrument, envelopes={})


def _state() -> DriverState:
    config = SimpleNamespace(psg_voice_map={"fTone_01": [_entry(_TONE_1)], "fTone_02": [_entry(_TONE_2)]},
                             psg_map={_FORM: _entry(_NOISE)})
    return DriverState(config, is_psg=True, psg=PsgTable(tuple(range(128))))


class NoiseRouting(unittest.TestCase):
    def test_noise_takes_over_and_gives_back_the_tone_instrument(self):
        st = _state()
        st.apply(PsgVoice("fTone_01"))
        st.apply(PsgForm(_FORM))
        self.assertEqual((st.instrument, st.in_noise_mode), (_NOISE, True))
        st.apply(PsgForm(None))
        self.assertEqual((st.instrument, st.in_noise_mode), (_TONE_1, False))

    def test_an_envelope_set_during_noise_routes_the_tones_after_it(self):
        st = _state()
        st.apply(PsgVoice("fTone_01"))
        st.apply(PsgForm(_FORM))
        st.apply(PsgVoice("fTone_02"))
        self.assertEqual(st.instrument, _NOISE)                  # the noise plays on with its envelope
        st.apply(PsgForm(None))
        self.assertEqual(st.instrument, _TONE_2)


class DetunedPsg(unittest.TestCase):
    _C4, _B7 = 48, 95
    _C3 = 36                              # Sonic 1's table's first note

    def _resolve(self, pitch: int, detune: int):
        entry = SimpleNamespace(mod_instrument=_TONE_1, envelopes={}, root=None, low=None, type="tone")
        config = SimpleNamespace(psg_voice_map={"fTone_01": [entry]}, psg_map={}, range_space="chip")
        st = DriverState(config, is_psg=True, psg=SONIC1_RULES.psg_table)
        st.apply(PsgVoice("fTone_01"))
        st.apply(Detune(detune))
        return resolve_note(st, pitch - self._C3, 0, "PSG1")

    def test_a_small_detune_stays_on_its_note(self):
        res = self._resolve(self._C4, 2)         # C4's 427 + 2: 8 c
        self.assertEqual((res.chip, res.key, res.detune), (self._C4, self._C4, 2))

    def test_a_large_one_sounds_as_its_note(self):
        res = self._resolve(self._B7, 567)       # B7's 29 + 567 = 596: F#3's 604 - 8
        self.assertEqual((res.chip, res.key, res.raw_index - res.source, res.detune), (42, 42, 42 - self._B7, -8))


if __name__ == "__main__":
    unittest.main()
