"""A log's bursts gathered into the driver's updates (core/vgm/spans.py), on hand-made bursts.

    python -m pytest tests/core/vgm/test_spans.py -q
"""

from __future__ import annotations

import unittest

from core.vgm.bursts import Burst, Tune
from core.vgm.clock import FrameClock
from core.vgm.spans import UpdateSpan, update_spans

_FRAME = 735
_CLOCK = FrameClock(_FRAME)             # frame n opens at n * 735, its burst due at +183
_DUE = _FRAME // 4
_FM1_FREQUENCY = 0xA4
_FM3, _FM5 = 2, 4                       # writes.track_of's


def _burst(first: int, last: int, writes: int = 4, opens: int | None = None, closes: int | None = None,
           tunes: tuple[Tune, ...] = ()) -> Burst:
    return Burst(first, last, writes, tunes, opens, closes)


class Spans(unittest.TestCase):
    def test_bursts_in_one_frame_are_one_update(self):
        spans = update_spans([_burst(_DUE, _DUE + 100), _burst(_DUE + 400, _DUE + 450)], _CLOCK)
        self.assertEqual(spans, [UpdateSpan(0, _DUE, _DUE + 450, 8)])

    def test_a_burst_due_in_the_next_frame_is_its_update(self):
        spans = update_spans([_burst(_DUE, _DUE + 100), _burst(_FRAME + _DUE, _FRAME + _DUE + 50)], _CLOCK)
        self.assertEqual([s.frame for s in spans], [0, 1])

    def test_a_burst_late_in_the_next_frame_goes_on_with_a_long_update(self):
        # The update pauses at +600 (the Z80 feeding the DAC); a burst at +950, past the next one's due
        spans = update_spans([_burst(_DUE, _DUE + 600), _burst(_DUE + 950, _DUE + 960)], _CLOCK)
        self.assertEqual(spans, [UpdateSpan(0, _DUE, _DUE + 960, 8)])

    def test_a_burst_carrying_on_the_track_order_goes_on_with_an_update_in_the_next_window(self):
        # Time Trax: FM3 written last at +600 (the next window open), FM5's tail 201 samples on, before
        # the next due by more than a quarter frame: the same pass over the tracks
        first = _burst(_DUE, _DUE + 600, closes=_FM3)
        spans = update_spans([first, _burst(_DUE + 801, _DUE + 810, opens=_FM5)], _CLOCK)
        self.assertEqual(spans, [UpdateSpan(0, _DUE, _DUE + 810, 8)])

    def test_a_burst_back_on_an_earlier_track_is_the_next_update(self):
        first = _burst(_DUE, _DUE + 600, closes=_FM5)
        self.assertEqual(len(update_spans([first, _burst(_DUE + 801, _DUE + 810, opens=_FM3)], _CLOCK)), 2)

    def test_an_update_ending_in_its_window_carries_on_nothing(self):
        first = _burst(_DUE, _DUE + 300, closes=_FM3)
        self.assertEqual(len(update_spans([first, _burst(_FRAME + 10, _FRAME + 20, opens=_FM5)], _CLOCK)), 2)

    def test_a_frequency_written_again_in_a_later_frame_starts_the_next_update(self):
        # One burst holding two updates: FM1's frequency at +0 and again at +740 (frame 1)
        again = _DUE + 740
        tunes = (Tune(0, _DUE, _DUE, _FM1_FREQUENCY), Tune(5, again, again - 10, _FM1_FREQUENCY))
        spans = update_spans([_burst(_DUE, again + 20, 8, tunes=tunes)], _CLOCK)
        self.assertEqual(spans, [UpdateSpan(0, _DUE, again - 10, 5), UpdateSpan(1, again, again + 20, 3)])

    def test_a_log_cut_at_resets_never_goes_on(self):
        clock = FrameClock(_FRAME, 0, (500, 1200))
        spans = update_spans([_burst(100, 450), _burst(900, 950)], clock)
        self.assertEqual([s.frame for s in spans], [0, 1])


class Clock(unittest.TestCase):
    def test_a_fixed_clock(self):
        clock = FrameClock(_FRAME, -100)
        self.assertEqual((clock.frame_of(-100), clock.frame_of(634), clock.frame_of(635)), (0, 0, 1))
        self.assertEqual((clock.opens(2), clock.burst_due(2)), (1370, 1370 + _DUE))

    def test_a_clock_cut_at_resets(self):
        clock = FrameClock(_FRAME, 0, (500, 1200))
        self.assertEqual([clock.frame_of(s) for s in (0, 500, 501, 1200, 1300)], [0, 0, 1, 1, 2])
        self.assertEqual([clock.opens(f) for f in range(3)], [0, 500, 1200])
        self.assertEqual(clock.burst_due(1), 500)
