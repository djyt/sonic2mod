"""The walk's state and MOD routing (core/plan/driver_state.py): a PSG track's instrument as its
envelope and noise form change, noise turned off again (Streets of Rage 2's `$F7 8`).

    python -m pytest tests/core/plan/test_driver_state.py -q
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.plan.driver_state import DriverState
from core.smps import PsgForm, PsgTable, PsgVoice

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


if __name__ == "__main__":
    unittest.main()
