"""Time Trax's volume envelopes (core/drivers/timetrax/envelope.py): read from the ROM, played as
$1421 steps them.

    python -m pytest tests/core/drivers/timetrax/test_envelope.py -q
"""

from __future__ import annotations

import unittest

from core.drivers.timetrax.envelope import Envelope, TimeTraxEnvelopes, read_envelopes
from core.drivers.timetrax.locate import driver_tables, time_trax_memory
from core.rom import RomImage
from tests.roms import TIME_TRAX_ROM, needs_time_trax


def _played(envelope: Envelope, frames: int) -> list[int]:
    run, volume, out = TimeTraxEnvelopes((envelope,)).start(0), 0, []
    for _ in range(frames):
        volume = run.frame(volume)
        out.append(volume)
    return out


class Ramps(unittest.TestCase):
    def test_slow_a_step_where_the_error_carries(self):
        # rise 2 over 4 frames: the segment's first frame, then a step every other frame, then the hold
        self.assertEqual(_played(Envelope(100, ((4, 102),), loops=False), 6), [100, 101, 101, 102, 102, 102])

    def test_fast_several_steps_a_frame(self):
        self.assertEqual(_played(Envelope(100, ((2, 106),), loops=False), 5), [100, 102, 105, 106, 106])

    def test_it_holds_or_loops(self):
        run = TimeTraxEnvelopes((Envelope(10, ((0, 20),), loops=False),)).start(0)
        run.frame(0)
        run.frame(10)
        self.assertEqual((run.frame(20), run.done), (20, True))
        self.assertEqual(_played(Envelope(10, ((0, 20), (0, 10)), loops=True), 6), [10, 20, 20, 10, 10, 20])


@needs_time_trax
class FromTheRom(unittest.TestCase):
    def test_its_28_envelopes(self):
        rom = RomImage.load(TIME_TRAX_ROM)
        envelopes = read_envelopes(time_trax_memory(rom), driver_tables(rom).envelopes).envelopes
        self.assertEqual(len(envelopes), 28)
        self.assertEqual(envelopes[0], Envelope(230, ((192, 200), (64, 245)), loops=False))
        self.assertTrue(envelopes[2].loops)
