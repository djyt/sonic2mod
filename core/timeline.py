"""Where a driver tick lands in the MOD, and how long it lasts.

The driver counts ticks; the MOD counts rows of `target_speed` MOD ticks at a BPM.  One row is
`ticks_per_row` driver ticks.  A tempo modifier m holds every m-th V-int frame (TempoWait), so a
frame count (smpsNoteFill, smpsModSet) is m-1 / m ticks a frame, and smpsSetTempoMod changes m
mid-song: the song is a list of tempo segments, each with its own BPM.

    tick ──► row = tick / ticks_per_row ──► (pattern, row) ──► (after mod_pattern_breaks) pattern
      └────► seconds: summed over the tempo segments the span crosses
"""

import bisect

from .config import ConversionConfig
from .mod import shift_for_breaks
from .smps_parser import SmpsSong

# A MOD BPM: ProTracker's Fxx reaches 32..255
_MIN_BPM, _MAX_BPM = 32, 255


class Timeline:
    def __init__(self, song: SmpsSong, config: ConversionConfig) -> None:
        self._song = song
        self._config = config
        self._segments: list[tuple[int, int]] = []

    @property
    def ticks_per_row(self) -> float:
        """Driver ticks per MOD row, the global tempo divider included.

        The parser stores note durations as raw_duration * chan_tempo_div (initialized to
        header.tempo_divider).  To convert stored ticks → rows we divide by yaml_tpr *
        global_divider, keeping BPM and YAML config unchanged.
        """
        return self._config.ticks_per_row * self._song.header.tempo_divider

    # --- tempo segments (smpsSetTempoMod, $EA) ------------------------------------------------
    @property
    def segments(self) -> list[tuple[int, int]]:
        """[(start tick, tempo modifier)] in tick order, the header's value first."""
        return self._segments or [(0, self._song.header.tempo_modifier)]

    def collect_segments(self) -> None:
        """Read the tempo segments off the song (again once its loops are replayed).

        cfSetTempo writes v_main_tempo for every track and restarts the TempoWait counter, so
        from that tick on ticks run at fps*(m-1)/m with the hold pattern starting afresh.
        Only the modifier changes here; the divider (smpsSetTempoDiv, $EB) is not applied.
        """
        segs = [(0, self._song.header.tempo_modifier)]
        for ch in self._song.channels:
            segs.extend((ev.tick_position, ev.effect.params[0]) for ev in ch.events
                        if ev.is_effect and ev.effect.effect_type == 'smpsSetTempoMod')
        segs.sort()
        out: list[tuple[int, int]] = []
        for t, m in segs:
            if out and out[-1][0] == t:
                out[-1] = (t, m)
            elif not out or out[-1][1] != m:
                out.append((t, m))
        self._segments = out

    def segment_at(self, tick) -> tuple[int, int]:
        """(start tick, tempo modifier) of the tempo segment `tick` falls in."""
        segs = self.segments
        i = bisect.bisect_right([s[0] for s in segs], tick) - 1
        return segs[max(i, 0)]

    def ticks_per_frame(self, modifier: int) -> float:
        """Duration ticks that elapse per V-int frame for a tempo modifier: (m - 1) / m.

        TempoWait fires once every `modifier` frames and only does `addq.b #1` on each
        track's DurationTimeout, cancelling that frame's decrement.  NoteTimeoutUpdate
        (smpsNoteFill) and DoModulation (smpsModSet wait/speed) still run on those frames,
        so they count FRAMES while note durations and tick positions count TICKS.  Multiply a
        frame count by this to place it on the converter's tick timeline.

        Region-independent (both clocks scale with fps).  SFX have no tempo modifier.  Use
        ticks_per_frame_at(tick) wherever the tick is known and this only for an explicitly
        chosen modifier.
        """
        if self._song.header.is_sfx or modifier <= 1:
            return 1.0
        return (modifier - 1) / modifier

    def ticks_per_frame_at(self, tick) -> float:
        return self.ticks_per_frame(self.segment_at(tick)[1])

    def bpm_for(self, modifier: int) -> int:
        """MOD BPM for a tempo modifier: the song's BPM scaled by the change in tick rate."""
        base = self.ticks_per_frame(self._song.header.tempo_modifier)
        return max(_MIN_BPM, min(_MAX_BPM, round(self._config.target_bpm * self.ticks_per_frame(modifier) / base)))

    # --- seconds ------------------------------------------------------------------------------
    def span_secs(self, start: float, end: float) -> float:
        """Seconds the MOD takes to play from tick `start` to tick `end`.

        A tick lasts target_speed x 2.5 / (BPM x ticks per row) seconds at the BPM in force,
        and smpsSetTempoMod changes that BPM mid-song (bpm_for, the rounded value the Fxx
        writes), so the span is summed over the tempo segments it crosses.
        """
        segs = self.segments
        total = 0.0
        for i, (seg_start, modifier) in enumerate(segs):
            seg_end = segs[i + 1][0] if i + 1 < len(segs) else float('inf')
            lo, hi = max(start, seg_start), min(end, seg_end)
            if hi > lo:
                total += ((hi - lo) * self._config.target_speed * 2.5
                          / (self.bpm_for(modifier) * self.ticks_per_row))
        return total

    def row_secs(self, tick) -> float:
        """Seconds one MOD row lasts at the BPM in force at `tick`."""
        return self._config.target_speed * 2.5 / self.bpm_for(self.segment_at(tick)[1])

    # --- rows and patterns --------------------------------------------------------------------
    def pattern_row(self, tick) -> tuple[int, int]:
        """(pattern, row) a tick rounds to, before any mod_pattern_breaks."""
        row_total = round(tick / self.ticks_per_row)
        return row_total // 64, row_total % 64

    def pattern_of(self, tick: int) -> int:
        """The pattern of the reference build (after its `mod_pattern_breaks`) a note-on at
        `tick` lands in — what a `merge_patterns:` group is matched on (core.merge)."""
        return shift_for_breaks(int(tick // self.ticks_per_row), self._config.mod_pattern_breaks or []) // 64

    def last_pattern(self) -> int:
        """The MOD's last pattern: the one the loop's `Bxx` row lands in; what convert.py trims
        the output to.  A loop extension may overshoot the song's end by a tick, and that note
        lands in a pattern nothing reaches."""
        row = max(round(self._song.end_tick() / self.ticks_per_row), 1) - 1
        return shift_for_breaks(row, self._config.mod_pattern_breaks or []) // 64
