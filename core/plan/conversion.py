"""One conversion's decisions on its config, kept off the config, which stays as read.

    config ─► rendering pitches ─► detune variants ─────────► merge plan (the merged build)
              (prepare_instruments: the song as written)       (core.merge: the ticks the MOD plays)

Each step makes a new ConversionPlan.  Every pass reads what a note plays (walk_channel) and what a
slot holds (the catalogues, the sample table) through it, so another pass, or a tool, starts over
from the config.

The steps share one sample table and one pitch map, which are the conversion's, not a step's: an
earlier plan sees the rows a later step added (a detune variant, a composite, a bank) and the
volumes the merged build set.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..config import ConversionConfig, SampleRow

if TYPE_CHECKING:
    from .detune import DetunePlan
    from .instruments import FmInstrument


class MergeRouting(Protocol):
    """What the walk and the catalogues read of the merged build's plan (core.merge.MergePlan)."""
    spliced: set[tuple[str, int]]
    dropped: set[int]
    mix_only: set[int]

    @property
    def instruments(self) -> set[int]: ...

    @property
    def fm_instruments(self) -> list[FmInstrument]: ...

    def instrument_at(self, source: str, tick: int, default: int) -> int: ...

    def gain_at(self, source: str, tick: int) -> float: ...


class SampleTable:
    """The sample_list a conversion plays: the config's rows (copies: a conversion sets their
    volumes) and the rows its steps add.  A slot plays its last row; its first is the sample it
    was named for.

        config  [5 drum.raw]
        table   [5 drum.raw] [23 lead dt-20] [5 merge FM1]      slot 5 plays the composite
    """

    def __init__(self, rows: list[SampleRow] | None = None) -> None:
        self._rows = list(rows or [])

    def __iter__(self) -> Iterator[SampleRow]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def add(self, row: SampleRow) -> None:
        self._rows.append(row)

    def discard(self, row: SampleRow) -> None:
        """This very row out of the table, where it is in it."""
        self._rows = [r for r in self._rows if r is not row]

    def row(self, slot: int, original: bool = False) -> SampleRow | None:
        """The row `slot` plays: the last naming it, or the first when `original` (the sample the
        slot was named for, kept aside for the mixer)."""
        rows = [r for r in self._rows if r.slot == slot]
        if not rows:
            return None
        return rows[0] if original else rows[-1]

    def rows(self) -> dict[int, SampleRow]:
        """{slot: the row it plays} for every slot named."""
        return {r.slot: r for r in self._rows}


@dataclass(frozen=True)
class RenderPitch:
    """Where a rooted entry's sample is rendered (core.plan.synth_roots)."""
    synth_root: int | None          # the pitch (SMPS semitone); None: the entry's `low`
    shift: int = 0                  # synth_root − the pitch `root` sounds: the sample's rate is
                                    # 2^(shift/12) times root's playback rate


@dataclass(frozen=True, eq=False)
class ConversionPlan:
    """A conversion's decisions so far: the module docstring."""
    config: ConversionConfig
    samples: SampleTable
    pitches: dict[int, RenderPitch] = field(default_factory=dict)   # id(map entry) -> its rendering pitch
    detune: DetunePlan | None = None
    merge: MergeRouting | None = None

    @classmethod
    def of(cls, config: ConversionConfig) -> ConversionPlan:
        """Nothing decided yet: the config's own entries, and a copy of its sample_list."""
        return cls(config, SampleTable([dataclasses.replace(r) for r in config.sample_list or []]))

    def pitch(self, entry) -> RenderPitch:
        """Where `entry`'s sample is rendered: as resolved, else as the config states it."""
        return self.pitches.get(id(entry)) or RenderPitch(entry.synth_root)

    def with_pitches(self, pitches: dict[int, RenderPitch]) -> ConversionPlan:
        return dataclasses.replace(self, pitches=pitches)

    def with_detune(self, detune: DetunePlan | None) -> ConversionPlan:
        return dataclasses.replace(self, detune=detune)

    def with_merge(self, merge: MergeRouting | None) -> ConversionPlan:
        return dataclasses.replace(self, merge=merge)
