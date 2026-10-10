"""The SN76489 PSG device: VGMPlay's SN76489 core (3rdparty/sn76489/) through ctypes.

The chip alone, as MAME keeps a device: registers in, samples out.  What drives it with a song's
envelopes and notes is core/synth; the chip's facts (clock, level law) are core/chips/psg.py.

    from core.chips.sn76489 import SN76489

    sn = SN76489(clock_rate=3_579_545, sample_rate=44100)
    sn.write_tone_freq(0, 253)
    samples = sn.render_samples(44100)
"""

from .wrapper import SN76489

__all__ = ["SN76489"]
