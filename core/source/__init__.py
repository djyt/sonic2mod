"""Where a song comes from: SMPS assembly (smps/) or a VGM rip lifted by vgm/.

    load.py  read_song: a song file -> SmpsSong, by its suffix
"""

from ..vgm import LiftOptions, VgmLiftError, is_vgm_path
from .load import read_song

__all__ = ["LiftOptions", "VgmLiftError", "is_vgm_path", "read_song"]
