"""Instrument slots for the composites: the fit, twins (same shape, other fill or level) giving
their slot up first, stand-ins taking a dropped composite's notes."""

from __future__ import annotations

from ..plan import fm_catalogue, psg_catalogue, walk_channel
from ..smps import source_map
from .model import LAST_MOD_NOTE, NO_SLOT, Composite, MergePlan
from .notes import CompositeKey


def fit_composites(plan: MergePlan, song, config, free: list[int],
                    drums: set[int] = frozenset(), fm_slots: set[int] = frozenset(),  # type: ignore[assignment]
                    reserve: int = 0) -> set[int]:
    """Give every composite a MOD instrument slot - a never-named slot, or one of an instrument
    the merged build no longer plays - dropping composites while they do not all fit.

    Dropping a composite hands its notes back to the primary's own instrument, which may be one
    of the slots on offer, and takes its mix sources out of the pinned set, so the fit is redone
    until it is stable; the composites dropped first are those whose primary instrument is
    played anyway (no new slot needed), then the least played.  A drum's slot (`drums`) is never
    reused; an FM mix source's (`fm_slots`) only by a pcm composite.  A banked composite takes
    no slot here (`core.merge.banks` packs them once mixed); `reserve` slots are held back for the
    banks, and whatever the fit leaves free is theirs too (`plan.spare_slots`).  Returns the
    instruments left unused by the final plan."""
    while True:
        unused = _unused_instruments(plan, song, config)
        sources = plan.pcm_sources                          # of the composites still in the plan
        slots = free + sorted(unused - (sources & drums))
        pcm_only = (unused & sources & fm_slots) - drums
        usable = slots[:max(0, len(slots) - reserve)]
        comps = sorted((c for c in plan.composites.values() if not c.banked), key=lambda c: (-c.notes, c.inst))
        chosen, left = _plan_slots(comps, usable, pcm_only)
        if not left:
            plan.slots_free = len(slots)
            plan.spare_slots = [s for s in slots if s not in chosen.values()]
            _assign_slots(plan, chosen)
            return unused
        cheap = {c.primary for c in comps} - unused          # primaries whose instrument stays anyway
        # A composite whose shape another has loses nothing when dropped (its twin stands in for
        # it), so the twins go first: Green Hill's lead chord in slots 25 and 31 differed only by
        # where PSG1's layer was cut, while five chords with no twin lost their followers.
        twins = same_shape_twins(plan, comps)
        for c in sorted(comps, key=lambda c: (c.inst not in twins, c.primary not in cheap, c.notes, -c.inst))[:len(left)]:
            drop_composite(plan, config, c, NO_SLOT, prefer=twins.get(c.inst))
        # Its notes move to a same-shape survivor now, not after the fit: counted as the
        # primary's own they kept its instrument's slot from the next fit (Green Hill's voice
        # $08 sample stayed installed with no note playing it)
        stand_in(plan)


def _plan_slots(comps: list[Composite], slots: list[int], pcm_only: set[int]
                ) -> tuple[dict[int, int], list[Composite]]:
    """Slots for the composites in order (the most played first): each takes the first slot it
    may hold — a chip-rendered one never a `pcm_only` slot.  ({composite id: slot}, the ones
    left without)."""
    pool = list(slots)
    chosen: dict[int, int] = {}
    left: list[Composite] = []
    for c in comps:
        pick = next((s for s in pool if c.fm is None or s not in pcm_only), None)
        if pick is None:
            left.append(c)
            continue
        pool.remove(pick)
        chosen[c.inst] = pick
    return chosen, left


def trigger_note(comp: Composite, primary_index: int) -> int:
    """The MOD note a pcm composite is triggered at for a primary note at `primary_index`:
    its own trigger note (the fastest layer's, or the primary's) moved by how far this note
    is from the pitch the mix was made at."""
    return (comp.note if comp.note is not None else comp.base) + (primary_index - comp.base)


def _shape(key: CompositeKey) -> tuple:
    """What a composite sounds like apart from its followers' fills and levels: a dropped one
    may stand in for another of the same shape (a kick+bass mix with or without the bass's
    67 ms pluck) rather than lose the follower's note."""
    return (key.kind, key.primary, tuple(lay.shape for lay in key.layers))


def _reach(key: CompositeKey) -> tuple[int, int]:
    """How far a composite's followers ring: how many are never cut, then the sum of the cuts
    (ms).  Of two composites of one shape, the one that reaches further holds the other's notes
    best: a layer ringing on under a short note is heard less than one cut from a long note."""
    fills = [lay.fill_ms for lay in key.layers]
    return (sum(f is None for f in fills), sum(f for f in fills if f is not None))


def same_shape_twins(plan: MergePlan, comps: list[Composite]) -> dict[int, CompositeKey]:
    """{composite id: the key of the one that would stand in for it} for every composite whose
    shape (_shape: its followers' fills and levels aside) another in `comps` has, and which
    that other can play every note of.  Of each shape the one that reaches furthest (_reach),
    then the most played, is the one kept."""
    by_shape: dict[tuple, list[Composite]] = {}
    for c in comps:
        by_shape.setdefault(_shape(c.key), []).append(c)
    out: dict[int, CompositeKey] = {}
    for same in by_shape.values():
        if len(same) < 2:
            continue
        keep = max(same, key=lambda c: (_reach(c.key), c.notes, -c.inst))
        for c in same:
            if c is not keep and _takes_all(plan, keep, c):
                out[c.inst] = keep.key
    return out


def _takes_all(plan: MergePlan, keep: Composite, c: Composite) -> bool:
    """True when `keep` can play every note of `c`: a mix is transposed to each note, and a
    trigger off the MOD's three octaves would lose it."""
    if keep.fm is not None:
        return True
    return all(0 <= trigger_note(keep, plan.bases.get(k, c.base)) <= LAST_MOD_NOTE
               for k, v in plan.ticks.items() if v == c.inst)


def drop_composite(plan: MergePlan, config, c: Composite, reason: str, prefer: CompositeKey | None = None) -> None:
    """Take a composite out of the plan: its notes play the primary alone (unless a composite
    of the same shape stands in for it, `stand_in`; `prefer` is the key of the one to take
    them when it survives), and it is reported."""
    plan.unsupported.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail, 'reason': reason,
                             'shape': _shape(c.key), 'group_label': c.group.label + c.group.where,
                             'longest': c.longest, 'prefer': prefer,
                             'ticks': [k for k, v in plan.ticks.items() if v == c.inst]})
    if c.entry in config.sample_list:
        config.sample_list.remove(c.entry)
    del plan.composites[c.key]
    plan.ticks = {k: v for k, v in plan.ticks.items() if v != c.inst}
    plan.notes = {k: n for k, n in plan.notes.items() if k in plan.ticks}


def stand_in(plan: MergePlan) -> None:
    """Every dropped composite whose shape a surviving one has plays that one instead: the
    follower's note is kept, with the other's fill and level.  Safe to call again after a
    later drop (core.merge.banks): an entry already settled is left alone."""
    by_shape: dict[tuple, Composite] = {}
    for c in sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst)):
        by_shape.setdefault(_shape(c.key), c)
    for u in plan.unsupported:
        if u.get('settled'):
            continue
        u['settled'] = True
        c = plan.composites.get(u['prefer']) if u.get('prefer') is not None else None
        if c is None:
            c = by_shape.get(u['shape'])
        if c is None or not u['ticks']:
            continue
        taken = 0
        for k in u['ticks']:
            if c.fm is None:
                base = plan.bases.get(k, c.base)
                trig = trigger_note(c, base)
                if not 0 <= trig <= LAST_MOD_NOTE:
                    continue                    # transposed off the MOD's range: stays lost
                if trig != base:
                    plan.notes[k] = trig
                else:
                    plan.notes.pop(k, None)
            plan.ticks[k] = c.inst
            taken += 1
        if taken:
            c.notes += taken
            c.longest = max(c.longest, u.get('longest', 0.0))
            c.uses[u['group_label']] = c.uses.get(u['group_label'], 0) + taken
            u['stand_in'] = c.inst


def cap_composites(plan: MergePlan, config) -> None:
    """Apply each group's max_composites: the most-played composites stay."""
    kept: dict[str, int] = {}
    for c in sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst)):
        cap = c.group.max_composites
        if cap is not None and kept.get(c.group.primary, 0) >= cap:
            drop_composite(plan, config, c, f"over the group's max_composites: {cap}")
        else:
            kept[c.group.primary] = kept.get(c.group.primary, 0) + 1


def _assign_slots(plan: MergePlan, chosen: dict[int, int]) -> None:
    """Give the composites their MOD instruments (`chosen`: {provisional id: slot}, from
    _plan_slots, which has one for each)."""
    order = sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst))
    # A banked composite keeps its provisional id until core.merge.banks packs it into a slot
    remap: dict[int, int | None] = {c.inst: (c.inst if c.banked else chosen.get(c.inst)) for c in order}
    for c in list(order):
        if c.banked:
            continue
        real = remap[c.inst]
        assert real is not None, "fit_composites gives every composite it keeps a slot"
        if c.entry is not None:
            c.entry[0] = real
        if c.fm is not None:
            c.fm.inst = real
        c.inst = real
    plan.ticks = {k: remap[v] for k, v in plan.ticks.items() if remap.get(v) is not None}  # type: ignore[misc]
    plan.notes = {k: n for k, n in plan.notes.items() if k in plan.ticks}


def _unused_instruments(plan: MergePlan, song, config) -> set[int]:
    """Instruments the merged build renders nothing for: named by the maps, the DAC mapping or
    the sample list, but played by no note of a channel that stays in the output."""
    used: set[int] = set(plan.instruments)
    smap = source_map(song)
    dac_map = {d.name: d.mod_instrument for d in config.dac_samples}
    for chan_cfg in config.channels:
        channel = smap.get(chan_cfg.source)
        if not chan_cfg.enabled or channel is None:
            continue
        for event, _st, res in walk_channel(channel, config, chan_cfg):
            if event.is_note and getattr(event, "merged", None) is None and plan.is_folded(chan_cfg.source, event.tick_position):
                continue                     # played on another channel (or dropped) in this pattern
            if res is not None:
                used.add(res.instrument)
            elif event.is_note and event.note.is_dac and event.note.dac_name in dac_map:
                used.add(plan.instrument_at(chan_cfg.source, event.tick_position, dac_map[event.note.dac_name]))
    named = set(fm_catalogue(song, config).instruments) | set(psg_catalogue(config))
    named |= set(dac_map.values()) | {e[0] for e in (config.sample_list or [])}
    return named - used
