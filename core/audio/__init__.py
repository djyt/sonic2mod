"""Sample arithmetic, with no SMPS and no MOD in it: dB, PCM, resampling, sustain loops.

    gain.py      dB <-> gain
    pcm.py       mono / int8 / raw16, DC block, treble shelf, a render's conditioning, dithered quantiser
    resample.py  polyphase windowed-sinc resampler (FM renders, PSG tones, mixes, SFX)
    loops.py     where a render settles, its best crossfaded loop, its release rate
    pitch.py     Hz <-> MIDI, note names (A4, C#4), cents
"""

from .gain import db_to_gain, gain_to_db, power_to_db
from .loops import (
    FLAT_DB,
    PROBE_SECS,
    RELEASE_FLOOR_DB,
    SustainLoop,
    apply_loop,
    fade_end,
    find_sustain_loop,
    heard_padding,
    probe_secs,
    release_rate_db_s,
    unroll_values,
)
from .pcm import (
    DEFAULT_DITHER,
    DITHER_FLAT,
    DITHER_MODES,
    DITHER_OFF,
    DITHER_SHAPED,
    INT8_PEAK,
    condition_render,
    dc_block,
    full_scale_int8,
    high_shelf,
    int8_to_raw16,
    limit_peaks,
    normalize_int8,
    peak,
    saturate,
    signed8,
    to_int8,
    to_mono,
    trim_trailing_silence,
    write_raw16,
)
from .pitch import NO_PITCH, cents, hz_to_midi, midi_name, pitch_name
from .resample import DEFAULT_BETA, DEFAULT_PHASES, DEFAULT_TAPS, build_kernel, resample, resample_stereo

__all__ = [
    "DEFAULT_BETA",
    "DEFAULT_DITHER",
    "DEFAULT_PHASES",
    "DEFAULT_TAPS",
    "DITHER_FLAT",
    "DITHER_MODES",
    "DITHER_OFF",
    "DITHER_SHAPED",
    "FLAT_DB",
    "INT8_PEAK",
    "NO_PITCH",
    "PROBE_SECS",
    "RELEASE_FLOOR_DB",
    "SustainLoop",
    "apply_loop",
    "build_kernel",
    "cents",
    "condition_render",
    "db_to_gain",
    "dc_block",
    "fade_end",
    "find_sustain_loop",
    "full_scale_int8",
    "gain_to_db",
    "heard_padding",
    "high_shelf",
    "hz_to_midi",
    "int8_to_raw16",
    "limit_peaks",
    "midi_name",
    "normalize_int8",
    "peak",
    "pitch_name",
    "power_to_db",
    "probe_secs",
    "release_rate_db_s",
    "resample",
    "resample_stereo",
    "saturate",
    "signed8",
    "to_int8",
    "to_mono",
    "trim_trailing_silence",
    "unroll_values",
    "write_raw16"
]
