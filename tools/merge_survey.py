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
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig
from core.convert import survey_context
from core.merge import PairStats
from core.plan import load_config


def survey(cfg: ConversionConfig, config_path: str) -> tuple[list[PairStats], dict[str, int]]:
    """PairStats for every ordered pair of enabled channels, and each channel's note count."""
    ctx = survey_context(cfg, config_path)
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
    cfg = load_config(args.config)
    stats, counts = survey(cfg, args.config)

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
