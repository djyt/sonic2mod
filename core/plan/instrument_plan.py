"""What the instruments are before anything is rendered: the decisions the converter makes on the
config, and what pitch each MOD instrument sounds once they are made.

    prepare_instruments   every rooted entry's rendering pitch (resolve_synth_roots), then the detune
                          variants the settings ask for (plan_detune_variants) - the converter's first
                          steps, and the audit tools' whole preparation, so the two cannot drift
    sounding_pitches      per MOD instrument: the note it is anchored at, the pitch that note sounds
                          and its sample's cents off it (an FM detune; a PSG table divider off equal
                          temperament), read from the catalogue the generators render from
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

from ..audio import semitone_to_hz
from ..chips import MD_PSG_CLOCK, psg_frequency_hz
from ..config import ConversionConfig
from ..smps import PSG_TABLE_PITCH
from .detune import DetunePlan, detune_cents, detune_variants_wanted, plan_detune_variants
from .instruments import RENDER_INDEX_C1, TONE, fm_catalogue, psg_catalogue
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
            out[inst] = InstrumentPitch(psg.root_idx, psg.root_semitone,
                                        _psg_divider_cents(psg.synth_idx + RENDER_INDEX_C1, song.rules.psg_frequencies))
    return out


def _psg_divider_cents(semitone: int, psg_frequencies: tuple[int, ...]) -> float:
    """Cents the driver's divider for a pitch sounds off equal temperament: the sample is
    rendered with it (Streets of Rage's and Sonic 1's B7: divider 29, -42 c)."""
    i = semitone - PSG_TABLE_PITCH
    if not 0 <= i < len(psg_frequencies) or psg_frequencies[i] <= 0:
        return 0.0
    return 1200 * math.log2(psg_frequency_hz(psg_frequencies[i], MD_PSG_CLOCK) / semitone_to_hz(semitone))
