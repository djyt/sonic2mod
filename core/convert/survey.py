"""A song prepared the way `convert.py --merged` prepares it before it plans the merge, with every
channel's notes in hand: what tools/merge_survey.py and tools/fold_csv.py pair and count.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..audio import db_to_gain
from ..config import ConversionConfig, find_settings, load_settings
from ..merge import NoteOn, PairStats, channel_notes, pair_channels
from ..plan import resolve_synth_roots
from .smps2mod import SmpsToModConverter


@dataclass
class SurveyContext:
    """A song walked as the merged build walks it, with every channel's notes in hand."""
    song: object
    conv: SmpsToModConverter
    sources: list[str]                               # the enabled channels, in config order
    notes: dict[str, tuple[dict[int, NoteOn], list[int]]]   # channel -> ({tick: NoteOn}, rests)
    level_scale: Callable[[NoteOn], float]
    tolerance: int

    @property
    def counts(self) -> dict[str, int]:
        return {src: len(self.notes[src][0]) for src in self.sources}

    def pattern_of(self, tick: int) -> int:
        """The reference build's pattern a note-on at `tick` lands in (after its breaks)."""
        return self.conv.pattern_of_tick(tick)

    @property
    def last_pattern(self) -> int:
        """The reference MOD's last pattern (the loop's Bxx row; convert.py trims after it)."""
        return self.conv.last_pattern()

    def pair(self, primary: str, follower: str, patterns=None, cut_primary: bool = False) -> PairStats:
        """The follower lined up with the primary, over the whole song or in `patterns` only."""
        p_notes, p_rests = self.restrict(primary, patterns)
        f_notes, f_rests = self.restrict(follower, patterns)
        return pair_channels(p_notes, p_rests, f_notes, f_rests, primary, follower, self.level_scale,
                             cut_primary=cut_primary, tolerance=self.tolerance)

    def restrict(self, source: str, patterns) -> tuple[dict[int, NoteOn], list[int]]:
        notes, rests = self.notes[source]
        if patterns is None:
            return notes, rests
        return ({t: n for t, n in notes.items() if self.pattern_of(t) in patterns},
                [r for r in rests if self.pattern_of(r) in patterns])


def survey_context(cfg: ConversionConfig, config_path: str) -> SurveyContext:
    """Parse and prepare the song the way `convert.py --merged` does before it builds the merge
    plan (tempo re-timing, loop extension, baked levels) and collect every channel's notes."""
    song = cfg.read_song()
    synth, psg = load_settings(find_settings(config_path))
    conv = SmpsToModConverter(song, cfg, synth=synth, psg_synth=psg)
    resolve_synth_roots(song, cfg)
    conv.prepare_song()                      # a replayed loop body is as many notes as it plays
    baselines = conv.level_baselines()

    def level_scale(n: NoteOn) -> float:
        base = baselines.get(n.kind, {}).get(n.instrument)
        if base is None or n.level_db is None:
            return 1.0
        return db_to_gain(n.level_db - base)

    sources = [c.source for c in cfg.channels if c.enabled]
    sample_secs = conv.sample_secs()
    tol = max(0, int(cfg.merge_tolerance))
    notes = {src: channel_notes(song, cfg, src, conv.pan_law_db, sample_secs, lambda t: conv.tick_span_secs(t, t + 1),
                                tol)
             for src in sources}
    return SurveyContext(song, conv, sources, notes, level_scale, tol)
