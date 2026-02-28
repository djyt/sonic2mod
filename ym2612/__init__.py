"""YM2612 / OPN2 emulation package.

Wraps the Nuked-OPN2 C library (reference/Nuked-OPN2/ym3438.c) via ctypes to
provide FM voice sample synthesis for sonic2mod.

Public API::

    from ym2612.wrapper import OPN2

    opn2 = OPN2()                           # builds DLL on first run, resets chip
    opn2.write_reg(0xB0, 0x07)              # algorithm 7, feedback 0, ch 0
    opn2.key_on(channel=0)
    samples = opn2.render_samples(53267)    # 1 second at native rate

    from ym2612 import render_note, note_to_freq, freq_to_fnum_block

    pcm, rate = render_note(voice, mod_note_index)   # 8-bit signed mono PCM
"""

from .wrapper  import OPN2
from .voice    import program_voice
from .renderer import render_note, note_to_freq, freq_to_fnum_block

__all__ = [
    "OPN2",
    "program_voice",
    "render_note",
    "note_to_freq",
    "freq_to_fnum_block",
]
