"""Notes spliced onto other channels: a follower's solo notes, and the fill pool placing notes on
whichever output channel is silent when they start."""

from __future__ import annotations

import bisect

from ..config import MergeGroup
from ..smps_song import SmpsEvent, SmpsNote
from ..tables import source_map
from .model import GroupNotes, MergePlan
from .notes import NoteOn, channel_notes


def splice_solo_notes(plan: MergePlan, song, g: MergeGroup, p_notes: dict[int, NoteOn], p_rests: list[int]) -> None:
    """Put every follower note that starts while the primary is silent into the primary's event
    stream as the follower's own note (walk_channel resolves it from the NoteOn), followed by
    the follower's rest where nothing of the primary takes the channel back."""
    channel = source_map(song)[g.primary]
    p_ticks = sorted(p_notes)
    events = channel.events
    for st in plan.stats_of(g):
        for t, n in sorted(st.solo_notes.items()):
            if (g.primary, t) in plan.solo:
                continue                                    # an earlier follower already took this tick
            nxt = bisect.bisect_right(p_ticks, t)
            span = (p_ticks[nxt] - t) if nxt < len(p_ticks) else 1 << 30
            _splice_note(plan, events, g.primary, st.follower, t, n, span)


def _splice_note(plan: MergePlan, events: list, target: str, source: str, t: int, n: NoteOn,
                 span: int) -> None:
    """Put note `n` of `source` into `target`'s event stream at tick t as the source's own note
    (walk_channel resolves it from the NoteOn), followed by a rest at its end where nothing of
    the target's takes the channel back within `span` ticks."""
    plan.solo[(target, t)] = (source, n)
    plan.spliced.update((source, tt) for tt in n.all_ticks)
    ev = SmpsEvent(note=SmpsNote(note_value=n.note_value, duration=n.duration), tick_position=t)
    ev.merged = n                                       # type: ignore[attr-defined]
    # A rest at this tick (the target's own, or the end of the solo note before) would put a
    # C00 or a release slide in the note's cell: the note re-keys the channel by itself
    events[:] = [e for e in events if not (e.tick_position == t and e.is_note and e.note.is_rest)]
    _insert_event(events, ev)
    end = t + n.duration
    own = {e.tick_position for e in events if e.is_note and (not e.note.is_rest or not e.note.is_no_attack)}
    if end not in own and span >= n.duration:
        rest = SmpsEvent(note=SmpsNote(note_value=0x80, duration=0, is_rest=True), tick_position=end)
        rest.merged = NoteOn(end, 0, 0, 0, 0, n.kind)  # type: ignore[attr-defined]
        _insert_event(events, rest)


def _occupancy(plan: MergePlan, song, config, source: str, pan_law_db: float, sample_secs, tick_secs,
               grace: int, pattern_of=None) -> list[tuple[int, int]]:
    """[(start, end)] ticks a live channel sounds: its own notes and every note spliced onto it.
    Where a group's `cut_after` (or the song-wide `merge_fill_cut_after`) holds, a note counts
    for that many ticks only: the pool may cut its tail."""
    def span(t: int, n: NoteOn) -> tuple[int, int]:
        end = t + n.sounding
        over = plan.cut_after_at(config, source, pattern_of(t) if pattern_of is not None else None)
        if over:
            end = min(end, t + over)
        return t, end
    notes, _rests = channel_notes(song, config, source, pan_law_db, sample_secs, tick_secs, grace)
    spans = [span(n.tick, n) for n in notes.values() if (source, n.tick) not in plan.folded]
    spans += [span(t, n) for (src, t), (_f, n) in plan.solo.items() if src == source]
    return sorted(spans)


def _free_span(spans: list[tuple[int, int]], t: int) -> int:
    """Ticks the channel stays silent from t: 0 when something sounds at t, else the time to
    the next span's start (spans may overlap: a hat inside a drum's decay)."""
    if any(s <= t < e for s, e in spans):
        return 0
    return min((s - t for s, _e in spans if s > t), default=1 << 30)


def pool_notes(plan: MergePlan, song, config, pan_law_db: float, sample_secs, tick_secs,
                grace: int, min_ticks: int, groups: list[GroupNotes], pattern_of=None) -> bool:
    """The fill pool: every note of a `merge_fill` channel, every lost follower note of a
    `fill_lost` group, and every follower note of a `fill_cut` group whose ring the fold would
    cut, onto whichever output channel is silent when it starts (the module docstring).  A
    cut note is only moved where a channel is silent for all of it - folded, it at least keeps
    its onset.  A `fill: true` group pools its primary's notes in the group's patterns (an
    arpeggio sprinkled over the bridge's four columns); those that find no column are lost and
    leave the channel's own column too (`plan.folded`).  A note never goes to a column nobody
    plays on in its pattern (a folded, dropped, moved-away or pooled channel's own column).  A
    pooled follower note leaves its group's notes (the caller re-pairs).  Records per-source
    counts in plan.fill; returns True when any follower note was pooled."""
    pool: list[tuple[int, int, str, NoteOn, bool]] = []      # (tick, order, source, note, whole note only)
    order = 0
    for src in config.merge_fill:
        notes, _ = channel_notes(song, config, src, pan_law_db, sample_secs, tick_secs, grace)
        pool += [(t, order, src, n, False) for t, n in notes.items()]
        order += 1
    for g in plan.groups:
        for st in plan.stats_of(g):
            if g.fill_lost:
                pool += [(t, order, st.follower, n, False) for t, n in st.lost_notes.items()]
            if g.fill_cut:
                pool += [(t, order, st.follower, n, True) for t, n in st.cut_notes.items()
                         if t not in st.lost_notes]
            order += 1
    filled: set[tuple[str, int]] = set()          # (source, tick) of every fill-group note
    for g in plan.groups:
        if g.fill and pattern_of is not None:
            notes, _ = channel_notes(song, config, g.primary, pan_law_db, sample_secs, tick_secs, grace)
            for t, n in notes.items():
                if g.covers(pattern_of(t)):
                    pool.append((t, order, g.primary, n, False))
                    filled.add((g.primary, t))
            order += 1
    if not pool:
        return False
    smap = source_map(song)
    live = sorted((c for c in config.channels if c.enabled), key=lambda c: c.mod_channel)
    spans = {c.source: _occupancy(plan, song, config, c.source, pan_law_db, sample_secs, tick_secs, grace,
                                  pattern_of)
             for c in live}
    # The solo notes the groups will splice onto their primaries occupy those channels too
    for st in plan.stats:
        if st.primary in spans:
            for t, n in st.solo_notes.items():
                bisect.insort(spans[st.primary], (t, t + n.sounding))
    follower_notes = {f: f_notes for gn in groups for f, f_notes, _ in gn.followers}
    stats: dict[str, dict] = {}
    pooled = False
    for t, _o, src, n, whole in sorted(pool, key=lambda x: (x[0], x[1])):
        st = stats.setdefault(src, {'source': src, 'notes': 0, 'placed': 0, 'cut': 0, 'lost': 0,
                                    'folded': 0, 'targets': {}})
        st['notes'] += 1
        best = None
        here = pattern_of(t) if pattern_of is not None else None
        for c in live:
            if c.source == src or (here is not None and here in plan.away_patterns(c.source)):
                continue                        # its own column, or one nobody plays on here
            free = _free_span(spans[c.source], t)
            need = n.sounding if whole else max(1, min(min_ticks, n.sounding))
            if free < need:
                continue
            fit = min(free, n.sounding)
            if best is None or fit > best[0]:
                best = (fit, c.source, free)
        if best is None:
            st['folded' if whole else 'lost'] += 1     # a cut note without a channel folds as before
            if (src, t) in filled:                       # a pooled channel's note: not on its own column either
                plan.folded.update((src, tt) for tt in n.all_ticks)
            continue
        fit, target, free = best
        _splice_note(plan, smap[target].events, target, src, t, n, free)
        over = plan.cut_after_at(config, target, here)   # a pool note on that column is cuttable likewise
        bisect.insort(spans[target], (t, min(t + fit, t + over) if over else t + fit))
        st['placed'] += 1
        st['cut'] += fit < n.sounding
        st['targets'][target] = st['targets'].get(target, 0) + 1
        if src in follower_notes:
            follower_notes[src].pop(t, None)     # no longer folded onto its primary
            pooled = True
    plan.fill = list(stats.values())
    return pooled


def _insert_event(events: list, ev) -> None:
    """Insert after every event at or before its tick (the primary's flags at that tick stay ahead)."""
    i = bisect.bisect_right([e.tick_position for e in events], ev.tick_position)
    events.insert(i, ev)
