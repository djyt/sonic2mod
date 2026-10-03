"""What each note plays and what each sample is rendered for: the song read through its config.

    driver_state.py  DriverState, resolve_note, walk_channel: the one answer every pass reads
    instruments.py   the instrument catalogue (first entry to name a slot wins)
    instrument_plan.py  prepare_instruments (synth roots + detune, as the converter and the tools make them),
                     sounding_pitches (what each instrument's root note sounds)
    detune.py        detune variants
    synth_roots.py   the pitch each rooted entry's sample is rendered at
    noise_derive.py  noise envelopes and rate-3 dividers
    timeline.py      driver tick -> MOD pattern / row / seconds
"""

from .detune import DetunePlan, DetuneVariant, detune_cents, detune_variants_wanted, plan_detune_variants
from .driver_state import DriverState, ResolvedNote, enabled_channels, walk_channel
from .instrument_plan import InstrumentPitch, InstrumentPlan, prepare_instruments, sounding_pitches
from .instruments import FmCatalogue, FmInstrument, FmLayer, PsgInstrument, fm_catalogue, free_slots, psg_catalogue
from .noise_derive import derive_noise_envelopes, derive_rate3_dividers
from .synth_roots import resolve_synth_roots
from .timeline import Timeline

__all__ = [
    "DetunePlan",
    "DetuneVariant",
    "DriverState",
    "FmCatalogue",
    "FmInstrument",
    "FmLayer",
    "InstrumentPitch",
    "InstrumentPlan",
    "PsgInstrument",
    "ResolvedNote",
    "Timeline",
    "derive_noise_envelopes",
    "derive_rate3_dividers",
    "detune_cents",
    "detune_variants_wanted",
    "enabled_channels",
    "fm_catalogue",
    "free_slots",
    "plan_detune_variants",
    "prepare_instruments",
    "psg_catalogue",
    "resolve_synth_roots",
    "sounding_pitches",
    "walk_channel"
]
