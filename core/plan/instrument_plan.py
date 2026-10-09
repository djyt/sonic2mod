"""What the instruments are before anything is rendered: the decisions the converter makes on the
config, and what pitch each MOD instrument sounds once they are made.

    prepare_instruments   every rooted entry's rendering pitch (resolve_synth_roots), then the detune
                          variants the settings ask for (plan_detune_variants) - the converter's first
                          steps, and the audit tools' whole preparation, so the two cannot drift
    sounding_pitches      per MOD instrument: the note it is anchored at, the pitch that note sounds
                          and its sample's detune, read from the catalogue the generators render from
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from ..config import ConversionConfig
from .detune import DetunePlan, detune_cents, detune_variants_wanted, plan_detune_variants
from .instruments import TONE, fm_catalogue, psg_catalogue
from .synth_roots import resolve_synth_roots


@dataclass
class InstrumentPlan:
    synth_roots: list[dict]         # resolve_synth_roots' report, one per rooted entry
    detune: DetunePlan | None       # None: FM is not synthesised or the settings want no variants


class InstrumentPitch(NamedTuple):
    root: int                       # the MOD note (index, C1 = 0) the instrument is anchored at
    root_semitone: int              # the pitch that note sounds (SMPS semitone, C0 = 0)
    cents: float                    # the sample's smpsAlterNote detune


def prepare_instruments(song, config: ConversionConfig, synth) -> InstrumentPlan:
    """Resolve the rendering pitches on `config`, then plan the detune variants (sets
    `config.detune_plan`).  `synth`: the SynthesisSettings, None where FM is not synthesised."""
    roots = resolve_synth_roots(song, config)
    detune = plan_detune_variants(song, config) if detune_variants_wanted(synth) else None
    return InstrumentPlan(roots, detune)


def sounding_pitches(song, config: ConversionConfig) -> dict[int, InstrumentPitch]:
    """Every rooted FM instrument and PSG tone instrument (noise has no pitch), first entry wins."""
    out: dict[int, InstrumentPitch] = {}
    for inst, fm in fm_catalogue(song, config).instruments.items():
        if fm.root_idx is None:
            continue
        offset = fm.layers[0].fnum_offset
        out[inst] = InstrumentPitch(fm.root_idx, fm.root_semitone,
                                    detune_cents(fm.rendered_semitone, offset, song.rules.fm_frequencies) if offset else 0.0)
    for inst, psg in psg_catalogue(config).items():
        if psg.entry.type == TONE and inst not in out:
            out[inst] = InstrumentPitch(psg.root_idx, psg.root_semitone, 0.0)
    return out
