"""smpsModSet → ProTracker 4xy: the speed x from the modulation's cycle, the depth y per note.

    smpsModSet wait, speed, delta, steps
        cycle = 2 * speed * (steps + 1) frames  ──►  x  (ProTracker's position advance per tick)
        swing = delta * steps / 2 FNUM / divider units  ──►  y  (per note: its own frequency word)

A cycle more than twice as slow as 4x1 (Streets of Rage's 251 steps: a sweep the note never
sees turn) is played as slides instead: each row slides to where the chip's pitch is at its end
(modulation_slides).
"""

from collections.abc import Callable

from ..chips import split_freq_word
from ..config import ConversionConfig
from ..diagnostics import Diagnostics, InfoKind
from ..plan import Timeline
from ..smps import ModSet, fm_table_index

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

# Slides: 1xx / 2xx on each tick after a row's first, E1x / E2x once
_SLIDE_UP, _SLIDE_DOWN, _EXTENDED = 0x1, 0x2, 0xE
_FINE_UP, _FINE_DOWN = 0x10, 0x20
_MAX_SLIDE = 0xFF
_CENTS_PER_OCTAVE = 1200


def vibrato_depth(delta: int, steps: int, period: int, chip_index: int, is_psg: bool,
                  psg_read: tuple[int, ...], fm_frequencies: tuple[int, ...], player: str = "ft2") -> int:
    """4xy depth nibble for one note in `player`'s replayer; 0 = too shallow to play.

    Driver: the accumulator swings delta * steps / 2 either side of its centre (first
    half-swing steps/2 steps, every later one the full `steps`), and is added to the note's
    own frequency word: the YM2612 FNUM of its pitch class in the song's table (the block is
    untouched) or the SN76489 divider of its PSG table entry.  An Amiga period and a PSG
    divider are both 1/f and a small FNUM change is proportional to f, so in every case the
    swing in periods is

        swing = period * (delta * steps / 2) / frequency_word

    The depth is the y whose peak in that player (_VIBRATO_PEAK) is nearest the swing: PT2
    truncates to whole periods, a round(swing / 2) depth there is a period short on every note
    (Green Hill's y=1 notes half as deep).  Below 0.7 periods the smallest depth would
    overshoot the hardware by 3x or more: no vibrato.
    """
    word = psg_read[chip_index & 0x7F] if is_psg else _fnum(chip_index, fm_frequencies)
    if word <= 0:
        return 0
    swing = period * (abs(signed_delta(delta)) * steps / 2) / word
    if swing < _MIN_SWING:
        return 0

    peak = _VIBRATO_PEAK[player]
    return min(range(1, _MAX_NIBBLE + 1), key=lambda y: abs(peak(y) - swing))


def signed_delta(delta: int) -> int:
    """A ModSet's delta, signed (Sonic 1's holds the raw byte)."""
    return delta - 0x100 if 0x80 <= delta <= 0xFF else delta


def modulation_offset(mod: ModSet, frame: int) -> int:
    """What the modulation adds to the frequency word (FNUM or PSG divider units) `frame`
    frames after the note's attack: nothing for `wait` frames, then `delta` every `speed`; it
    turns after half its steps, then after every `steps`.  Streets of Rage $90 FM4 (wait 8, +8
    a step, speed 1): the rip's word moves 0 ... 0 +8 +16 ..., +8 at frame 9.

        offset ^       /^^\\
               |     /      \\        /
               |___/          \\    /
               | wait           \\/
    """
    if frame <= mod.wait:
        return 0
    taken = (frame - mod.wait) // (mod.speed or 256)
    half = max(1, mod.steps // 2)
    if taken <= half:
        return signed_delta(mod.delta) * taken

    # A triangle of `steps` each way, down from the half swing first
    leg, at = divmod(taken - half, max(1, mod.steps))
    units = half - at if leg % 2 == 0 else half - mod.steps + at
    return signed_delta(mod.delta) * units


def modulation_slides(mod: ModSet, period: int, rows: list[tuple[float, float]], frame_of: Callable[[float], float],
                      cents_of: Callable[[int], float], row_ticks: int) -> list[tuple[float, int, int]]:
    """(row tick, effect, param): each row's slide to the chip's pitch at the row's end.

    `rows`: (start, end) ticks of the note's rows; `frame_of`: a tick's frame from the attack;
    `cents_of`: the pitch an offset moves the note by.  A row slides with 1xx / 2xx on its
    `row_ticks` later ticks, under one period a tick with E1x / E2x once.  What the MOD reached is
    carried on, so rounding never accumulates."""
    reached, out = float(period), []
    for start, end in rows:
        target = period * 2 ** (-cents_of(modulation_offset(mod, int(frame_of(end)))) / _CENTS_PER_OCTAVE)
        move = reached - target                 # periods to slide: > 0 up (the period falls)
        per_tick = min(round(abs(move) / row_ticks), _MAX_SLIDE)
        if per_tick:
            effect, param, moved = (_SLIDE_UP if move > 0 else _SLIDE_DOWN), per_tick, per_tick * row_ticks
        else:
            fine = min(round(abs(move)), _MAX_NIBBLE)
            if not fine:
                continue
            effect, param, moved = _EXTENDED, (_FINE_UP if move > 0 else _FINE_DOWN) | fine, fine
        reached -= moved if move > 0 else -moved
        out.append((start, effect, param))
    return out


def _fnum(semitone: int, fm_frequencies: tuple[int, ...]) -> int:
    """The FNUM a semitone (nC0 = 0) plays in the song's FM table."""
    i = max(0, min(len(fm_frequencies) - 1, fm_table_index(semitone)))
    return split_freq_word(fm_frequencies[i])[0]


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
        if self._config.target_speed - 1 < 1:
            return 0
        exact = self._exact(mod_speed, steps, tick)
        x = max(1, min(_MAX_NIBBLE, round(exact)))
        if exact <= _MAX_NIBBLE + 0.5:
            return x

        # 4Fy is the fastest there is; say so once per channel and setting
        key = (channel, mod_speed, steps)
        if key not in self._rate_limited:
            self._rate_limited.add(key)
            tpf = self._timeline.ticks_per_frame_at(tick)
            played = _POSITIONS * self._timeline.ticks_per_row / ((self._config.target_speed - 1) * _MAX_NIBBLE * tpf)
            self._diag.info(InfoKind.VIBRATO_RATE_LIMIT, channel=channel,
                            wanted_cycle_frames=self._cycle_frames(mod_speed, steps), played_cycle_frames=played)
        return x

    def too_slow(self, mod_speed: int, steps: int, tick=0) -> bool:
        """The modulation cycles more than twice as slowly as 4x1 can: slides (modulation_slides)."""
        return self._config.target_speed > 1 and round(self._exact(mod_speed, steps, tick)) == 0

    def _exact(self, mod_speed: int, steps: int, tick) -> float:
        """The 4xy speed whose cycle is the modulation's, unrounded."""
        return (_POSITIONS * self._timeline.ticks_per_row
                / ((self._config.target_speed - 1) * self._cycle_frames(mod_speed, steps)
                   * self._timeline.ticks_per_frame_at(tick)))

    @staticmethod
    def _cycle_frames(mod_speed: int, steps: int) -> int:
        return 2 * (mod_speed or 256) * (steps + 1)
