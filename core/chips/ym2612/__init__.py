"""The YM2612 / OPN2 device: Nuked-OPN2 (3rdparty/nuked-opn2/ym3438.c) through ctypes.

The chip alone, as MAME keeps a device: registers in, samples out.  What drives it with a song's
voices and notes is core/synth; the chip's facts (clock, pitch, level laws) are core/chips/fm.py.

    from core.chips.ym2612 import OPN2

    opn2 = OPN2()                           # builds the library on first use (build/), resets the chip
    opn2.write_reg(0xB0, 0x07)              # algorithm 7, feedback 0, ch 0
    opn2.key_on(channel=0)
    samples = opn2.render_samples(53267)    # 1 second at native rate
"""

from .wrapper import OPN2, output_rate

__all__ = ["OPN2", "output_rate"]
