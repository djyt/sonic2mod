"""Sample arithmetic, with no SMPS and no MOD in it: dB, PCM, resampling, sustain loops.

    gain.py      dB <-> gain
    pcm.py       mono / int8 / raw16, DC block, treble shelf, dithered quantiser
    resample.py  polyphase windowed-sinc resampler (FM renders, PSG tones, mixes, SFX)
    loops.py     where a render settles, its best crossfaded loop, its release rate
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
    dc_block,
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
from .resample import DEFAULT_BETA, DEFAULT_PHASES, DEFAULT_TAPS, build_kernel, resample, resample_stereo

__all__ = [
    "DEFAULT_BETA", "DEFAULT_DITHER", "DEFAULT_PHASES", "DEFAULT_TAPS", "DITHER_FLAT", "DITHER_MODES", "DITHER_OFF",
    "DITHER_SHAPED", "FLAT_DB", "INT8_PEAK", "PROBE_SECS", "RELEASE_FLOOR_DB", "SustainLoop", "apply_loop",
    "build_kernel", "db_to_gain", "dc_block", "fade_end", "find_sustain_loop", "gain_to_db", "heard_padding",
    "high_shelf", "int8_to_raw16", "limit_peaks", "normalize_int8", "peak", "power_to_db", "release_rate_db_s",
    "resample", "resample_stereo", "saturate", "signed8", "to_int8", "to_mono", "trim_trailing_silence",
    "unroll_values", "write_raw16"
]
