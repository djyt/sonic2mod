"""The conversion: SmpsToModConverter and the passes it runs.

    smps2mod.py        SmpsToModConverter: song -> ModFile
    generators.py      SampleGenerators: the chip renderers convert.py hands in
    level_plan.py      baked levels, FM render levels
    sustain_plan.py    how long each sample holds
    vibrato.py         smpsModSet -> 4xy
    channel_writer.py  one channel into MOD cells
    layout.py          C00 / Fxx / Bxx / Dxx in the cells left free
"""

from .generators import SampleGenerators
from .smps2mod import SmpsToModConverter
from .vibrato import S1_FNUM_BASE, vibrato_depth

__all__ = [
    "S1_FNUM_BASE", "SampleGenerators", "SmpsToModConverter", "vibrato_depth"
]
