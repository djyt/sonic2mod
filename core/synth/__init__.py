"""The converter's sample rendering: a song's voices and envelopes driven through the chip devices
(core/chips/ym2612, core/chips/sn76489) into the PCM its MOD instruments play.  A layer above
core.plan (the instrument catalogue it renders) and below core.convert (which calls it).

    fm_voice.py     an SmpsVoice -> YM2612 register writes (the SMPS operator order)
    fm_render.py    a voice at a note (or a frame-by-frame track, layers) -> PCM
    fm_samples.py   generate_fm_samples / generate_fm_drums: every FM instrument of a song
    psg_render.py   a PSG divider or noise mode -> PCM; note_to_psg_n
    psg_samples.py  generate_psg_samples: every PSG instrument of a song

Each generator returns {MOD instrument: (int8 PCM bytes, sample rate)}; renders are kept in the
render cache (core/render_cache.py), salted with the device and renderer code they ran through.
"""

from .fm_render import freq_to_fnum_block, note_to_freq, render_layers, render_note, render_note_raw
from .fm_samples import generate_fm_drums, generate_fm_samples
from .fm_voice import program_voice
from .psg_render import note_to_psg_n, render_psg_noise, render_psg_noise_raw, render_psg_tone, render_psg_tone_raw
from .psg_samples import generate_psg_samples

__all__ = [
    "freq_to_fnum_block",
    "generate_fm_drums",
    "generate_fm_samples",
    "generate_psg_samples",
    "note_to_freq",
    "note_to_psg_n",
    "program_voice",
    "render_layers",
    "render_note",
    "render_note_raw",
    "render_psg_noise",
    "render_psg_noise_raw",
    "render_psg_tone",
    "render_psg_tone_raw",
]
