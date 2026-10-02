#!/usr/bin/env python3
"""Which of a song's channels can fold onto one MOD channel (the `merge:` groups)?

For every ordered pair of enabled channels (primary, follower) this lines the follower's
note-ons up with the primary's the way core/merge/ will, and prints the counts:

    paired      follower notes that merge into a composite instrument (same tick, not shorter)
    solo        follower notes that start while the primary is silent — placed on the merged
                channel as the follower's own note (cut = a primary note-on re-takes the channel)
    alone       primary notes with the follower resting (fine: the primary plays as before)
    orphans     follower notes that start while the primary sounds — LOST on the merged channel,
                unless the group says `cut_primary: true`: then they play and cut the primary's
                tail (a hi-hat over a drum's decay), and the drum channel is usually the place
    held        primary note-ons under a follower note that keeps sounding — its ring is lost
    shorter     follower notes at the primary's tick that end sooner — keyed off early inside
                the composite (paired, not lost)
    truncated   follower notes cut by the primary's rest
    vibrato     pairs whose modulation state differs (the primary's vibrato applies)
    composites  distinct composite instruments the pair needs (MOD slots)

A pair is `clean` when nothing is lost.  The suggestion at the end takes the cleanest pairs
first, each channel in one group only, and prints the YAML to paste into the config; pairs
with orphans are never suggested (that follower needs its own channel).

    python tools/merge_survey.py configs/01_title_screen.yaml
    python tools/merge_survey.py configs/01_title_screen.yaml --all     # every pair, not just the clean ones
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig, PsgSynthesisSettings, SynthesisSettings
from core.driver_state import resolve_synth_roots
from core.levels import db_to_gain
from core.merge import NoteOn, PairStats, channel_notes, pair_channels
from core.smps2mod import SmpsToModConverter
from core.smps_parser import SmpsParser


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


def survey_context(cfg: ConversionConfig, settings_dir: Path) -> SurveyContext:
    """Parse and prepare the song the way `convert.py --merged` does before it builds the merge
    plan (tempo re-timing, loop extension, baked levels) and collect every channel's notes."""
    song = SmpsParser().parse_file(cfg.input_file)
    settings = settings_dir / "settings.yaml"
    synth = SynthesisSettings.from_yaml(str(settings)) if settings.exists() else SynthesisSettings()
    psg = PsgSynthesisSettings.from_yaml(str(settings)) if settings.exists() else PsgSynthesisSettings()
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


def survey(cfg: ConversionConfig, settings_dir: Path) -> tuple[list[PairStats], dict[str, int]]:
    """PairStats for every ordered pair of enabled channels, and each channel's note count."""
    ctx = survey_context(cfg, settings_dir)
    counts = ctx.counts
    stats = []
    for p in ctx.sources:
        for f in ctx.sources:
            if p == f or not counts[f]:
                continue
            stats.append(ctx.pair(p, f))
    return stats, counts


def suggest(stats: list[PairStats]) -> list[tuple[str, list[str]]]:
    """Groups from the cleanest pairs: no orphans, most notes folded, each channel used once."""
    ranked = sorted((s for s in stats if s.orphans == 0 and (s.paired or s.solo)),
                    key=lambda s: (s.lost, -(s.paired + s.solo), len(s.keys)))
    used: set[str] = set()
    groups: dict[str, list[str]] = {}
    for s in ranked:
        if s.follower in used or s.follower in groups or s.primary in used:
            continue
        groups.setdefault(s.primary, []).append(s.follower)
        used.add(s.follower)
    return list(groups.items())


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("config")
    ap.add_argument("--all", action="store_true", help="print every pair, not only the clean ones")
    args = ap.parse_args()
    cfg = ConversionConfig.from_yaml(args.config)
    stats, counts = survey(cfg, Path(args.config).resolve().parent)

    print(f"{cfg.name}: " + ", ".join(f"{s} {n}" for s, n in counts.items()) + " notes\n")
    head = (f"{'primary':8} {'follower':8} {'notes':>5} {'paired':>6} {'solo':>4} {'cut':>3} {'alone':>5} "
            f"{'orphan':>6} {'held':>4} {'short':>5} {'trunc':>5} {'vib':>3} {'comps':>5}  verdict")
    print(head)
    print("-" * len(head))
    for s in sorted(stats, key=lambda s: (s.lost > 0, -s.paired, s.primary, s.follower)):
        if not args.all and not s.clean:
            continue
        verdict = ("clean" if s.clean else
                   "orphans: own channel, or cut_primary" if s.orphans else
                   "folds with losses")
        print(f"{s.primary:8} {s.follower:8} {s.follower_notes:5d} {s.paired:6d} {s.solo:4d} {s.solo_cut:3d} "
              f"{s.alone:5d} {s.orphans:6d} {s.held:4d} {s.shorter:5d} {s.truncated:5d} {s.vibrato:3d} "
              f"{len(s.keys):5d}  {verdict}")
    if not args.all:
        hidden = sum(1 for s in stats if not s.clean)
        if hidden:
            print(f"({hidden} pairs with losses hidden; --all shows them)")

    groups = suggest(stats)
    print()
    if not groups:
        print("No pair folds cleanly.")
        return
    live = [c.source for c in cfg.channels if c.enabled]
    folded = {f for _, fs in groups for f in fs}
    print(f"Suggested: {len(live)} channels -> {len(live) - len(folded)}\n")
    print("merge:")
    for p, fs in groups:
        print(f"  - primary: {p}")
        print(f"    followers: [{', '.join(fs)}]")


if __name__ == "__main__":
    main()
