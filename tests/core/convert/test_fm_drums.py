"""How long each FM drum rings (core/convert/fm_drums.py): until the drum track's next hit.

    python -m pytest tests/core/convert/test_fm_drums.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.convert.fm_drums import drum_rings
from core.drivers.reference import SONIC1_RULES
from core.smps import FmDrum, SmpsChannel, SmpsChannelHeader, SmpsEvent, SmpsNote, SmpsSong, SmpsSongHeader, SmpsVoice


def _drum_rings(loop_tick: int | None) -> dict[int, float]:
    """drum_rings on a drum track hitting drum81 at 0 and 40, drum82 at 10, resting at 30; ten
    ticks a second."""
    events = [SmpsEvent(SmpsNote(0x81, 10, is_dac=True, dac_name="drum81"), tick_position=0),
              SmpsEvent(SmpsNote(0x82, 20, is_dac=True, dac_name="drum82"), tick_position=10),
              SmpsEvent(SmpsNote(0x80, 10, is_rest=True), tick_position=30),
              SmpsEvent(SmpsNote(0x81, 10, is_dac=True, dac_name="drum81"), tick_position=40)]
    drums = SmpsChannel(header=SmpsChannelHeader(channel_type="DAC", label="drums"), events=events,
                        has_jump=loop_tick is not None, loop_tick=loop_tick, rules=SONIC1_RULES)
    song = SmpsSong(header=SmpsSongHeader(channels=[drums.header]), channels=[drums], rules=SONIC1_RULES)
    song.fm_drums = {name: FmDrum(SmpsVoice(0), 0, ()) for name in ("drum81", "drum82")}
    config = SimpleNamespace(dac_samples=[SimpleNamespace(name="drum81", mod_instrument=1, mod_note="C3"),
                                          SimpleNamespace(name="drum82", mod_instrument=2, mod_note="C3")])
    timeline = SimpleNamespace(span_secs=lambda start, end: (end - start) / 10)
    return drum_rings(song, config, timeline)


class DrumRings(unittest.TestCase):
    def test_a_drum_is_heard_until_the_drum_tracks_next_hit(self):
        # Hits at 0 (drum81), 10 (drum82), 40 (drum81), the song ending at 50; a rest between does not stop one
        self.assertEqual(_drum_rings(loop_tick=None), {1: 1.0, 2: 3.0})

    def test_the_last_hit_rings_across_the_loop(self):
        # The track jumps back to tick 5: the hit at 40 rings to the end (1.0), then 5 -> 10 (0.5)
        self.assertEqual(_drum_rings(loop_tick=5), {1: 1.5, 2: 3.0})


if __name__ == "__main__":
    unittest.main()
