"""One conversion's decisions on its config, kept off the config, which stays as read.

    config ─► rendering pitches ─► detune variants ─────────► merge plan (the merged build)
              (prepare_instruments: the song as written)       (core.merge: the ticks the MOD plays)

Each step makes a new ConversionPlan over the same sample table: the conversion's own copy of the
config's sample_list, which the steps add rows to (a detune variant, a composite, a bank) and the
merged build sets volumes in.  Every pass reads what a note plays (walk_channel) and what a slot
holds (the catalogues, the table) through it, so another pass, or a tool, starts over from the config.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..config import ConversionConfig, SampleTable

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


@dataclass(frozen=True)
class RenderPitch:
    """Where a rooted entry's sample is rendered (core.plan.synth_roots)."""
    synth_root: int | None          # the pitch (SMPS semitone); None: the entry's `low`
    shift: int = 0                  # synth_root − the pitch `root` sounds: the sample's rate is
                                    # 2^(shift/12) times root's playback rate


@dataclass(frozen=True, eq=False)
class ConversionPlan:
    config: ConversionConfig
    samples: SampleTable
    pitches: dict[int, RenderPitch] = field(default_factory=dict)   # id(map entry) -> its rendering pitch
    detune: DetunePlan | None = None
    merge: MergeRouting | None = None

    @classmethod
    def of(cls, config: ConversionConfig) -> ConversionPlan:
        """Nothing decided yet: the config's own entries and sample_list."""
        return cls(config, config.sample_table())

    def pitch(self, entry) -> RenderPitch:
        """Where `entry`'s sample is rendered: as resolved, else as the config states it."""
        return self.pitches.get(id(entry)) or RenderPitch(entry.synth_root)

    def with_pitches(self, pitches: dict[int, RenderPitch]) -> ConversionPlan:
        return dataclasses.replace(self, pitches=pitches)

    def with_detune(self, detune: DetunePlan | None) -> ConversionPlan:
        return dataclasses.replace(self, detune=detune)

    def with_merge(self, merge: MergeRouting | None) -> ConversionPlan:
        return dataclasses.replace(self, merge=merge)
