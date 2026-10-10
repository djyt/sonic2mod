"""OutRun: an SMPS Z80 driver on Timer A (Golden Axe's layout, Sonic 1's flags, voices and DPCM).

    variant.py    the description: flags by kind of track, the envelope commands
    layout.py     its header (the DAC track first) and Sonic 1's voice
    locate.py     the banks and the sound header, from the code and the 68k's RAM writes -> SoundIndex
"""

from .variant import OUTRUN

__all__ = ["OUTRUN"]
