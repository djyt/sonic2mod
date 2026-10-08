"""The sample generators the converter calls, as core sees them.

core cannot import the chip packages that render its samples: they import core (the config, the
instrument catalogue, audio).  It states what it needs here; convert.py hands the implementations in.

    convert.py ── SampleGenerators(fm=ym2612..., psg=sn76489..., fm_drums=ym2612...) ──► SmpsToModConverter
                                                                                           │ calls
    ym2612/  sn76489/ ── implement ──► FmGenerator / PsgGenerator / FmDrumGenerator ◄──────┘
         │
         └── import ──► core.plan, core.config, core.smps, core.audio

Each returns {MOD instrument: (int8 PCM bytes, sample rate)}.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..audio import SustainLoop
from ..config import ConversionConfig, PsgSynthesisSettings, SynthesisSettings
from ..plan import FmDrumInstrument, FmInstrument
from ..smps import PsgEnvelope, SmpsSong


class FmGenerator(Protocol):
    """ym2612.sample_generator.generate_fm_samples."""

    def __call__(self, song: SmpsSong, config: ConversionConfig, synth: SynthesisSettings, *,
                 tl_offsets: dict[int, int] | None = ...,
                 peaks_out: dict[int, tuple[int, int, float]] | None = ...,
                 raw_out: dict[int, tuple] | None = ...,
                 loops: bool = ...,
                 loops_out: dict[int, SustainLoop] | None = ...,
                 release_out: dict[int, float | None] | None = ...,
                 cache_out: dict[str, int] | None = ...,
                 extra: Sequence[FmInstrument] = ...) -> dict: ...


class PsgGenerator(Protocol):
    """sn76489.sample_generator.generate_psg_samples."""

    def __call__(self, config: ConversionConfig, psg_synth: PsgSynthesisSettings, *,
                 rate3_dividers: dict | None = ...,
                 noise_envelopes: dict | None = ...,
                 psg_envelopes: Mapping[str, PsgEnvelope] | None = ...,
                 loops: bool = ...,
                 loops_out: dict[int, SustainLoop] | None = ...,
                 raw_out: dict[int, tuple] | None = ...,
                 cache_out: dict[str, int] | None = ...) -> dict: ...


class FmDrumGenerator(Protocol):
    """ym2612.sample_generator.generate_fm_drums: a drum track's FM drum programs, rendered whole."""

    def __call__(self, drums: Sequence[FmDrumInstrument], synth: SynthesisSettings, frame_hz: float,
                 ring_secs: Mapping[int, float], cache_out: dict[str, int] | None = ...) -> dict: ...


@dataclass(frozen=True)
class SampleGenerators:
    """One generator per chip (and the FM drums); None where the caller renders nothing."""

    fm: FmGenerator | None = None
    psg: PsgGenerator | None = None
    fm_drums: FmDrumGenerator | None = None
