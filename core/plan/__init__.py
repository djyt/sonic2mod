"""What each note plays and what each sample is rendered for: the song read through its config.

    driver_state.py  DriverState, resolve_note, walk_channel: the one answer every pass reads
    instruments.py   the instrument catalogue (first entry to name a slot wins)
    detune.py        detune variants
    synth_roots.py   the pitch each rooted entry's sample is rendered at
    noise_derive.py  noise envelopes and rate-3 dividers
    timeline.py      driver tick -> MOD pattern / row / seconds
"""

from .detune import DetunePlan, DetuneVariant, detune_cents, detune_variants_wanted, plan_detune_variants
from .driver_state import DriverState, ResolvedNote, enabled_channels, walk_channel
from .instruments import FmCatalogue, FmInstrument, FmLayer, fm_catalogue, free_slots, psg_catalogue
from .noise_derive import derive_noise_envelopes, derive_rate3_dividers
from .synth_roots import resolve_synth_roots
from .timeline import Timeline

__all__ = [
    "DetunePlan", "DetuneVariant", "DriverState", "FmCatalogue", "FmInstrument", "FmLayer", "ResolvedNote",
    "Timeline", "derive_noise_envelopes", "derive_rate3_dividers", "detune_cents", "detune_variants_wanted",
    "enabled_channels", "fm_catalogue", "free_slots", "plan_detune_variants", "psg_catalogue",
    "resolve_synth_roots", "walk_channel"
]
