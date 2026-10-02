"""The sample generators the converter calls, as core sees them.

core cannot import the chip packages that render its samples: they import core (the config, the
instrument catalogue, audio).  It states what it needs here; convert.py hands the implementations in.

    convert.py ── SampleGenerators(fm=ym2612..., psg=sn76489...) ──► SmpsToModConverter
                                                                         │ calls
    ym2612/  sn76489/ ── implement ──► FmGenerator / PsgGenerator ◄──────┘
         │
         └── import ──► core.plan, core.config, core.smps, core.audio

Both return {MOD instrument: (int8 PCM bytes, sample rate)}.
"""

from dataclasses import dataclass
from typing import Protocol

from ..audio import SustainLoop
from ..config import ConversionConfig, PsgSynthesisSettings, SynthesisSettings
from ..smps import SmpsSong


class FmGenerator(Protocol):
    """ym2612.sample_generator.generate_fm_samples."""

    def __call__(self, song: SmpsSong, config: ConversionConfig, synth: SynthesisSettings, *,
                 tl_offsets: dict[int, int] | None = ...,
                 peaks_out: dict[int, tuple[int, int]] | None = ...,
                 raw_out: dict[int, tuple] | None = ...,
                 loops: bool = ...,
                 loops_out: dict[int, SustainLoop] | None = ...,
                 release_out: dict[int, float | None] | None = ...) -> dict: ...


class PsgGenerator(Protocol):
    """sn76489.sample_generator.generate_psg_samples."""

    def __call__(self, config: ConversionConfig, psg_synth: PsgSynthesisSettings, *,
                 rate3_dividers: dict | None = ...,
                 noise_envelopes: dict | None = ...,
                 loops: bool = ...,
                 loops_out: dict[int, SustainLoop] | None = ...,
                 raw_out: dict[int, tuple] | None = ...) -> dict: ...


@dataclass(frozen=True)
class SampleGenerators:
    """One generator per chip; None where the caller renders nothing."""

    fm: FmGenerator | None = None
    psg: PsgGenerator | None = None
