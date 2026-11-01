"""A song prepared the way `convert.py --merged` prepares it before it plans the merge, with every
channel's notes in hand: what tools/config/merge_survey.py and tools/config/fold_csv.py pair and count.
The config is set up as the converter's (prepare_conversion: auto_bpm, the song's overrides), so a
note's seconds are the ones the merged build plans with.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..audio import db_to_gain
from ..config import ConversionConfig
from ..merge import NoteOn, PairStats, channel_notes, pair_channels
from .prepare import prepare_conversion
from .smps2mod import SmpsToModConverter


@dataclass
class SurveyContext:
    """A song walked as the merged build walks it, with every channel's notes in hand."""
    song: object
    converter: SmpsToModConverter
    sources: list[str]                               # the enabled channels, in config order
    notes: dict[str, tuple[dict[int, NoteOn], list[int]]]   # channel -> ({tick: NoteOn}, rests)
    level_scale: Callable[[NoteOn], float]
    tolerance: int

    @property
    def counts(self) -> dict[str, int]:
        return {src: len(self.notes[src][0]) for src in self.sources}

    def pattern_of(self, tick: int) -> int:
        """The reference build's pattern a note-on at `tick` lands in (after its breaks)."""
        return self.converter.pattern_of_tick(tick)

    @property
    def last_pattern(self) -> int:
        """The reference MOD's last pattern (the loop's Bxx row; convert.py trims after it)."""
        return self.converter.last_pattern()

    def pair(self, primary: str, follower: str, patterns=None, cut_primary: bool = False) -> PairStats:
        """The follower lined up with the primary, over the whole song or in `patterns` only."""
        p_notes, p_rests = self.restrict(primary, patterns)
        f_notes, _ = self.restrict(follower, patterns)
        return pair_channels(p_notes, p_rests, f_notes, primary, follower, self.level_scale,
                             cut_primary=cut_primary, tolerance=self.tolerance)

    def restrict(self, source: str, patterns) -> tuple[dict[int, NoteOn], list[int]]:
        notes, rests = self.notes[source]
        if patterns is None:
            return notes, rests
        return ({t: n for t, n in notes.items() if self.pattern_of(t) in patterns},
                [r for r in rests if self.pattern_of(r) in patterns])


def survey_context(cfg: ConversionConfig, config_path: str) -> SurveyContext:
    """Parse and prepare the song the way `convert.py --merged` does before it builds the merge
    plan (tempo re-timing, loop extension, baked levels) and collect every channel's notes.  The
    followers stay on: the survey pairs every channel, before any fold."""
    prep = prepare_conversion(cfg, config_path)
    cfg, song, synth = prep.config, prep.song, prep.synth
    converter = SmpsToModConverter(song, cfg, synth=synth, psg_synth=prep.psg_synth)
    conv = converter.plan_instruments()      # synth roots and detune variants, as the converter's
    converter.prepare_song()                 # a replayed loop body is as many notes as it plays
    song = converter.song
    baselines = converter.level_baselines()

    def level_scale(n: NoteOn) -> float:
        base = baselines.get(n.kind, {}).get(n.instrument)
        if base is None or n.level_db is None:
            return 1.0
        return db_to_gain(n.level_db - base)

    sources = [c.source for c in cfg.channels if c.enabled]
    sample_secs = converter.sample_secs()
    tol = max(0, cfg.merge_tolerance)
    notes = {src: channel_notes(song, conv, src, converter.pan_law_db, sample_secs,
                                lambda t: converter.tick_span_secs(t, t + 1), tol)
             for src in sources}
    return SurveyContext(song, converter, sources, notes, level_scale, tol)
