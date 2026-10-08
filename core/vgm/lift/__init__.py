"""A frame log lifted back into the song the driver played: FrameLog -> SmpsSong (Route A).

    song.py   lift_song: the steps in order, LiftOptions, VgmLiftError, LIFTED_ASPECTS

Plan and status: docs/todo/vgz_conversion.md, Phase 1.  The only part of vgm/ that reads smps/.
"""

from .song import LIFTED_ASPECTS, LiftOptions, VgmLiftError, lift_song

__all__ = ["LIFTED_ASPECTS", "LiftOptions", "VgmLiftError", "lift_song"]
