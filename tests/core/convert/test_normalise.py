"""core/convert/normalise.py: the gain that lifts a song's loudest note to 64, and the volumes it raises.

    python -m pytest tests/core/convert/test_normalise.py -q
"""

from __future__ import annotations

import unittest

from core.config import SampleRow
from core.convert.normalise import normalise_gain, raise_volumes
from core.mod import ModFile
from core.plan import SampleTable


class NormaliseGain(unittest.TestCase):
    def test_the_loudest_note_is_lifted_to_64(self):
        self.assertAlmostEqual(normalise_gain(52.0), 64 / 52)

    def test_a_song_already_at_64_or_clamped_past_it_is_left(self):
        for peak in (64.0, 80.0, 0.0):
            self.assertEqual(normalise_gain(peak), 1.0, peak)


class RaiseVolumes(unittest.TestCase):
    def test_rows_and_headers_are_raised_alike_and_empty_slots_left(self):
        mod = ModFile(channels=4)
        for slot, volume in ((1, 52), (2, 4)):
            mod.samples[slot - 1].length = 8
            mod.samples[slot - 1].set_volume(volume)
        mod.samples[2].set_volume(10)                       # slot 3: no sample
        table = SampleTable([SampleRow(1, "dac81.raw", 52), SampleRow(2, "fm_v00_C3.raw", 4)])
        raise_volumes(mod, table, 64 / 52)
        self.assertEqual([r.volume for r in table], [64, 5])
        self.assertEqual([s.volume for s in mod.samples[:3]], [64, 5, 10])
