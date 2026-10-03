"""A converted MOD measured against its VGM/VGZ recording.

    pitch.py    symbolic: every chip note's pitch against the MOD note sounding there (no audio)
    render.py   each chip / MOD channel rendered alone (VGMPlay, ffmpeg + libopenmpt)
    signal.py   measures on those renders: level, spectra, partials, onsets, pitch tracks, vibrato
    levels.py   per-instrument level error and the sample_list volumes that zero it
    onsets.py   key-ons / audio onsets paired one to one
"""

from .levels import LEVEL_MAX_ERR, LEVEL_MAX_SPREAD, instrument_levels, mod_note_events, suggest_volumes, write_volumes
from .onsets import OnsetMatch, audio_onsets, keyon_onsets, onset_match
from .pitch import (
    audit_pitches,
    audit_settings,
    instrument_verdicts,
    mod_pitch_timeline,
    note_start_offset,
    prepare_audit,
)
from .render import (
    SR,
    VGM_CHANNELS,
    find_vgmplay,
    group_masks,
    reference_render_key,
    render_mod_channels,
    render_vgm_channels,
)
from .signal import (
    VIB_MIN_NOTE,
    band_profile,
    db,
    envelope_offset,
    harmonic_cents,
    load_wav,
    onsets,
    pitch_track,
    rms,
    seg_at,
    spectrum,
    vibrato_estimate,
    vibrato_estimates,
)

__all__ = [
    "LEVEL_MAX_ERR", "LEVEL_MAX_SPREAD", "SR", "VGM_CHANNELS", "VIB_MIN_NOTE", "OnsetMatch", "audio_onsets",
    "audit_pitches", "audit_settings", "band_profile", "db", "envelope_offset", "find_vgmplay", "group_masks", "harmonic_cents",
    "instrument_levels", "instrument_verdicts", "keyon_onsets", "load_wav", "mod_note_events", "mod_pitch_timeline",
    "note_start_offset", "onset_match", "onsets", "pitch_track", "prepare_audit", "reference_render_key", "render_mod_channels",
    "render_vgm_channels", "rms", "seg_at", "spectrum", "suggest_volumes", "vibrato_estimate", "vibrato_estimates", "write_volumes"
]
