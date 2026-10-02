"""smpsModSet → ProTracker 4xy: the speed x from the modulation's cycle, the depth y per note.

    smpsModSet wait, speed, delta, steps
        cycle = 2 * speed * (steps + 1) frames  ──►  x  (ProTracker's position advance per tick)
        swing = delta * steps / 2 FNUM / divider units  ──►  y  (per note: its own frequency word)
"""

from ..config import ConversionConfig
from ..diagnostics import Diagnostics, InfoKind
from ..plan import Timeline
from ..smps import PSG_FREQUENCIES_EXTENDED

# Sonic 1 base FNUM for note C, from the MakeFMFrequency table (644 for C ... 1216 for B).
# The 11-bit FNUM is the same across all octave blocks — block just shifts the register.
# smpsModSet adds its swing to the note's own FNUM: see vibrato_depth.
S1_FNUM_BASE = 644

# A 4xy's swing at its sine table's peak (255), in Amiga periods, per player (settings.yaml `player`):
#   PT2  (255 * y) >> 7 whole periods             2y - 1      y=1: 1   y=2: 3     y=3: 5
#   FT2  (255 * y) >> 5 quarter periods, i.e. /4  2y - 1/4    y=1: 1.75 y=2: 3.75 y=3: 5.75
_VIBRATO_PEAK = {
    "pt2": lambda y: (255 * y) >> 7,
    "ft2": lambda y: ((255 * y) >> 5) / 4,
}

# ProTracker's vibrato position wraps at 64; 4Fy is the fastest there is
_POSITIONS = 64
_MAX_NIBBLE = 0xF

# Below this swing (periods) the smallest depth would overshoot the hardware by 3x or more
_MIN_SWING = 0.7


def vibrato_depth(delta: int, steps: int, period: int, chip_index: int, is_psg: bool,
                  player: str = "ft2") -> int:
    """4xy depth nibble for one note in `player`'s replayer; 0 = too shallow to play.

    Driver: the accumulator swings delta * steps / 2 either side of its centre (first
    half-swing steps/2 steps, every later one the full `steps`), and is added to the note's
    own frequency word: the YM2612 FNUM of its pitch class (644 for C ... 1216 for B, the
    block is untouched) or the SN76489 divider of its PSGFrequencies entry.  An Amiga period
    and a PSG divider are both 1/f and a small FNUM change is proportional to f, so in every
    case the swing in periods is

        swing = period * (delta * steps / 2) / frequency_word

    The depth is the y whose peak in that player (_VIBRATO_PEAK) is nearest the swing: PT2
    truncates to whole periods, a round(swing / 2) depth there is a period short on every note
    (Green Hill's y=1 notes half as deep).  Below 0.7 periods the smallest depth would
    overshoot the hardware by 3x or more: no vibrato.
    """
    word = (PSG_FREQUENCIES_EXTENDED[chip_index & 0x7F] if is_psg
            else S1_FNUM_BASE * 2 ** ((chip_index % 12) / 12))
    if word <= 0:
        return 0
    if delta >= 0x80:
        delta -= 0x100
    swing = period * (abs(delta) * steps / 2) / word
    if swing < _MIN_SWING:
        return 0

    peak = _VIBRATO_PEAK[player]
    return min(range(1, _MAX_NIBBLE + 1), key=lambda y: abs(peak(y) - swing))


class VibratoSpeed:
    """The 4xy speed of each smpsModSet, reporting once per channel and setting where 4Fy is too slow."""

    def __init__(self, timeline: Timeline, config: ConversionConfig, diag: Diagnostics) -> None:
        self._timeline = timeline
        self._config = config
        self._diag = diag
        self._rate_limited: set = set()

    def speed(self, mod_speed: int, steps: int, channel: str, tick=0) -> int:
        """ProTracker 4xy speed nibble for an smpsModSet (speed, steps) pair; 0 = cannot be played.

        Driver (DoModulation, once per V-int frame): every `speed` frames the delta is added; when
        the step counter runs out it is reloaded from the ORIGINAL steps byte, the delta is negated
        and one more step is spent.  Only the first half-swing uses the halved count, so the steady
        cycle is 2 * speed * (steps + 1) frames.  Not multiplied by the tempo divider.

        ProTracker: the vibrato position advances by x on each of a row's (speed - 1) processing
        ticks and wraps at 64.  One row is ticks_per_row driver ticks = ticks_per_row /
        ticks_per_frame_at(tick) frames, so matching the two cycle lengths gives

            x = 64 * ticks_per_row / ((target_speed - 1) * cycle_frames * ticks_per_frame_at(tick))

        Region-independent: both clocks scale with the frame rate.
        """
        rows_ticks = self._config.target_speed - 1
        if rows_ticks < 1:
            return 0
        cycle_frames = 2 * (mod_speed or 256) * (steps + 1)
        tpf = self._timeline.ticks_per_frame_at(tick)
        tpr = self._timeline.ticks_per_row
        exact = _POSITIONS * tpr / (rows_ticks * cycle_frames * tpf)
        x = max(1, min(_MAX_NIBBLE, round(exact)))
        if exact <= _MAX_NIBBLE + 0.5:
            return x

        # 4Fy is the fastest there is; say so once per channel and setting
        key = (channel, mod_speed, steps)
        if key not in self._rate_limited:
            self._rate_limited.add(key)
            played = _POSITIONS * tpr / (rows_ticks * _MAX_NIBBLE * tpf)
            self._diag.info(InfoKind.VIBRATO_RATE_LIMIT, channel=channel, wanted_cycle_frames=cycle_frames,
                            played_cycle_frames=played)
        return x
