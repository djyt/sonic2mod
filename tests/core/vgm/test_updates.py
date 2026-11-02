"""The driver's updates timed by their bursts (core/vgm/updates.py): a V-int lost where an update
runs past the next V-int and none follows in that frame.  On hand-built logs.

    python -m pytest tests/core/vgm/test_updates.py -q
"""

from __future__ import annotations

import unittest
from collections.abc import Collection

from core.vgm import FrameCut, decode_vgm, driver_updates, frame_log, realigned
from tests.vgm_build import BURST, FRAME, bursts, fm, key, with_writes

_FRAMES = 12
_LONG = 5                       # the frame whose update runs long
_STEP = 150                     # samples between a long update's writes: one burst
_CUT_RESET = (0x27, 0x15)       # Timer A's flag reset: the update's last write
_FNUM_HI = 0xA4                 # FM1's frequency: written once an update
_NEAR = FRAME - 60              # samples past an update's start: just short of the next V-int


def _writes(skipped: Collection[int] = ()):
    """FM1 keyed off every frame but `skipped`, a burst each."""
    return decode_vgm(bursts({f: key(0, False) for f in range(_FRAMES) if f not in skipped}, _FRAMES))


def _log(skipped: Collection[int] = (), long_to: int | None = None, tuned: Collection[int] = ()):
    """_writes, frame _LONG's update written on to `long_to` samples past its start; FM1's frequency
    written at each of `tuned` samples past it."""
    log = _writes(skipped)
    start = _LONG * FRAME + BURST
    if long_to is not None:
        log = with_writes(log, [*range(start + _STEP, start + long_to, _STEP), start + long_to])
    log = with_writes(log, [start + t for t in tuned], _FNUM_HI)
    return frame_log(log)


def _by_frame(fl):
    """Each update by the frame its first burst was written for (the log's frames run a window ahead)."""
    return {fl.frame_of(f * FRAME + BURST): f for f in range(_FRAMES)}


class Updates(unittest.TestCase):
    def test_a_burst_every_frame_is_an_update_a_frame_apart(self):
        fl = _log()
        updates = driver_updates(fl)
        self.assertEqual(len(updates), _FRAMES)
        self.assertEqual({u.gap for u in updates[1:]}, {FRAME})
        self.assertEqual({u.gap_frames for u in updates[1:]}, {1.0})
        self.assertEqual({u.late for u in updates}, {0})
        self.assertFalse(any(u.overran or u.lost for u in updates))
        self.assertIsNone(updates[0].gap)

    def test_a_long_update_then_none_for_a_frame_lost_a_v_int(self):
        fl = _log(skipped={_LONG + 1}, long_to=FRAME + _STEP)
        updates = {_by_frame(fl)[u.frame]: u for u in driver_updates(fl)}
        self.assertTrue(updates[_LONG].overran)
        after = updates[_LONG + 2]
        self.assertEqual((after.gap, after.lost, after.before), (2 * FRAME, True, updates[_LONG].frame))
        self.assertEqual(sum(u.lost for u in updates.values()), 1)

    def test_a_long_update_ending_just_short_then_one_frame_with_none_lost_a_v_int(self):
        fl = _log(skipped={_LONG + 1}, long_to=_NEAR)
        updates = {_by_frame(fl)[u.frame]: u for u in driver_updates(fl)}
        self.assertFalse(updates[_LONG].overran)
        self.assertEqual((updates[_LONG].reach, updates[_LONG + 2].lost), (_NEAR - FRAME, True))

    def test_a_long_update_ending_just_short_then_an_idle_stretch_may_have(self):
        fl = _log(skipped={_LONG + 1, _LONG + 2, _LONG + 3}, long_to=_NEAR)
        after = next(u for u in driver_updates(fl) if u.skipped)
        self.assertEqual((after.skipped, after.lost, after.possible), (3, False, True))

    def test_a_frequency_written_again_past_the_v_int_is_the_next_update_run_on(self):
        # Frame _LONG's burst runs into the next frame, writing FM1's frequency again there: the next
        # frame's update, no frame without one
        start = _LONG * FRAME + BURST
        retuned = FRAME + _STEP + 1
        updates = driver_updates(_log(skipped={_LONG + 1}, long_to=FRAME + _STEP, tuned=(0, retuned)))
        self.assertEqual(len(updates), _FRAMES)
        self.assertFalse(any(u.lost or u.possible for u in updates))
        long = next(i for i, u in enumerate(updates) if u.first == start)
        self.assertEqual((updates[long].last, updates[long + 1].first), (start + FRAME + _STEP, start + retuned))

    def test_an_idle_frame_after_a_short_update_is_no_loss(self):
        fl = _log(skipped={_LONG + 1})
        updates = {_by_frame(fl)[u.frame]: u for u in driver_updates(fl)}
        self.assertEqual((updates[_LONG + 2].gap_frames, updates[_LONG + 2].lost), (2.0, False))

    def test_a_burst_late_in_the_next_frame_goes_on_with_the_long_update(self):
        # Frame _LONG's writes run to +600, then pause; a burst at +950 (frame _LONG + 1's due + 215):
        # the update going on, not frame _LONG + 1's
        start = _LONG * FRAME + BURST
        base = _writes({_LONG + 1})
        fl = frame_log(with_writes(base, [start + 300, start + 450, start + 600, start + 950]))
        self.assertEqual(len(driver_updates(fl)), len(driver_updates(frame_log(base))))
        long = next(u for u in driver_updates(fl) if u.first == start)
        self.assertEqual((long.last, long.writes, long.overran), (start + 950, 5, True))

    def test_a_late_burst_writing_a_frequency_again_is_an_update_of_its_own(self):
        # As above, the late burst writing FM1's frequency again: frame _LONG + 1's own update
        start = _LONG * FRAME + BURST
        base = with_writes(_writes({_LONG + 1}), [start], _FNUM_HI)
        long = with_writes(base, [start + 300, start + 450, start + 600])
        late = with_writes(long, [start + 950], _FNUM_HI)
        self.assertEqual(len(driver_updates(frame_log(late))), len(driver_updates(frame_log(long))) + 1)

    def test_a_log_cut_at_resets_is_an_update_a_frame(self):
        body = key(0, False) + fm(0, *_CUT_RESET)
        fl = frame_log(decode_vgm(bursts({f: body for f in range(4)}, 4)), cut=FrameCut.TIMER_A)
        self.assertEqual([u.frame for u in driver_updates(fl)], [0, 1, 2, 3])
        self.assertFalse(any(u.overran or u.lost for u in driver_updates(fl)))

    def test_a_realigned_log_has_none(self):
        fl = _log()
        self.assertEqual(driver_updates(realigned(fl, {_LONG: -1})), [])
