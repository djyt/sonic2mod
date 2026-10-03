"""Where a song comes from: SMPS assembly (smps/), a ROM's bytecode (rom/) or a VGM rip lifted by vgm/.

    load.py  read_song: a song file -> SmpsSong, by its suffix
"""

from ..rom import RomError, is_rom_path
from ..vgm import LiftOptions, VgmLiftError, is_vgm_path
from .load import read_song

__all__ = ["LiftOptions", "RomError", "VgmLiftError", "is_rom_path", "is_vgm_path", "read_song"]
