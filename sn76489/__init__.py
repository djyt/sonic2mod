"""SN76489 PSG emulation package.

Wraps the VGMPlay SN76489 C library (reference/SN76489/) via ctypes to
provide PSG voice sample synthesis for sonic2mod.

Public API::

    from sn76489.wrapper import SN76489
    from sn76489 import (
        render_psg_tone, render_psg_tone_raw,
        render_psg_noise, render_psg_noise_raw,
        generate_psg_samples,
        note_to_psg_n,
    )
"""

from .wrapper          import SN76489
from .renderer         import (
    render_psg_tone, render_psg_tone_raw,
    render_psg_noise, render_psg_noise_raw,
    note_to_psg_n,
)
from .sample_generator import generate_psg_samples

__all__ = [
    "SN76489",
    "render_psg_tone",
    "render_psg_tone_raw",
    "render_psg_noise",
    "render_psg_noise_raw",
    "generate_psg_samples",
    "note_to_psg_n",
]
