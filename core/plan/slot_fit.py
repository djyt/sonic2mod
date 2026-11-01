"""A minimal config's instruments fitted to a MOD's 31 slots (core.plan.derive): a voice's chip
pitches cut into windows, a sample each, and what gives way when the windows outnumber the slots.

    windows    the distinct pitches in runs, each at most max_span(its lowest) wide
    volumes    an envelope the volume shapes (Streets of Rage 2's ADSR: one per preset and volume)
               plays at the volume most of its notes take: the least played first, Cxx elsewhere
    merges     two neighbouring windows of a voice as one, the cheapest first: the fewest notes
               taken from their own window's sample

            C2 .. G2   C4 .. A4          C2 ........ A4        one sample: the C4-A4 notes play
            (40)       (6)          ->   (46)                  it from further off
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Hashable, Mapping
from typing import TypeVar

Window = tuple[int, int]            # lowest and highest chip pitch
MaxSpan = Callable[[int], int]      # the widest window from a pitch
K = TypeVar("K", bound=Hashable)    # what a group of pitches is keyed by


def windows(pitches: Counter, max_span: MaxSpan, merges: int = 0, widest: int | None = None) -> list[Window]:
    """The distinct `pitches` cut into runs, each spanning at most `max_span(its lowest pitch)`;
    then `merges` neighbouring pairs joined, the cheapest first (none past `widest`)."""
    ps = sorted(set(pitches))
    out, start = [], 0
    while start < len(ps):
        end, span = start, max_span(ps[start])
        while end + 1 < len(ps) and ps[end + 1] - ps[start] <= span:
            end += 1
        out.append((ps[start], ps[end]))
        start = end + 1
    for _ in range(merges):
        pair = _cheapest(out, pitches, widest)
        if pair is None:
            break
        out[pair:pair + 2] = [(out[pair][0], out[pair + 1][1])]
    return out


def merge_windows(groups: Mapping[K, Counter], max_span: MaxSpan, widest: int, slots: int) -> dict[K, int]:
    """{group: its windows merged}: the cheapest merge of any group, one at a time, until the
    groups' windows fit `slots` (or none can merge)."""
    merges: dict[K, int] = {}

    def cut(key: K) -> list[Window]:
        return windows(groups[key], max_span, merges.get(key, 0), widest)

    total = sum(len(cut(key)) for key in groups)
    while total > slots:
        costs = {key: cost for key in groups if (cost := _merge_cost(cut(key), groups[key], widest)) is not None}
        if not costs:
            break
        key = min(costs, key=lambda k: (costs[k], str(k)))
        merges[key] = merges.get(key, 0) + 1
        total -= 1
    return merges


def fold_volumes(tone: Mapping[str, Counter], slots: int, family: Callable[[str], str | None],
                 count: Callable[[Mapping[str, Counter]], int]) -> tuple[dict[str, Counter], dict[str, tuple[str, int]]]:
    """`tone` with each envelope at a volume folded onto the same envelope's most played volume
    (`family`: the envelope a name plays at a volume; None: one the volume does not shape), the
    least played first, while `count` of it exceeds `slots`; and {folded: (what it plays as, notes)}."""
    tone = {label: Counter(pitches) for label, pitches in tone.items()}
    folded: dict[str, tuple[str, int]] = {}
    while count(tone) > slots:
        heads = _most_played(tone, family)
        spares = [label for label in tone if (f := family(label)) is not None and heads[f] != label]
        if not spares:
            break
        label = min(spares, key=lambda lb: (tone[lb].total(), lb))
        head = heads[family(label)]          # type: ignore[index]
        folded[label] = (head, tone[label].total())
        tone[head].update(tone.pop(label))
    return tone, folded


def _most_played(tone: Mapping[str, Counter], family: Callable[[str], str | None]) -> dict[str, str]:
    """{family: its most played member}."""
    heads: dict[str, str] = {}
    for label in sorted(tone):
        f = family(label)
        if f is not None and (f not in heads or tone[label].total() > tone[heads[f]].total()):
            heads[f] = label
    return heads


def _merge_cost(cut: list[Window], pitches: Counter, widest: int) -> int | None:
    """The notes the cheapest merge of `cut` moves; None: no pair may merge."""
    pair = _cheapest(cut, pitches, widest)
    return None if pair is None else _pair_cost(cut, pair, pitches)


def _cheapest(cut: list[Window], pitches: Counter, widest: int | None) -> int | None:
    """The pair (its first window's index) whose merge moves the fewest notes: the lighter window's."""
    fits = [i for i in range(len(cut) - 1) if widest is None or cut[i + 1][1] - cut[i][0] <= widest]
    return min(fits, key=lambda i: (_pair_cost(cut, i, pitches), i), default=None)


def _pair_cost(cut: list[Window], i: int, pitches: Counter) -> int:
    return min(_notes(cut[i], pitches), _notes(cut[i + 1], pitches))


def _notes(window: Window, pitches: Counter) -> int:
    lo, hi = window
    return sum(n for p, n in pitches.items() if lo <= p <= hi)
