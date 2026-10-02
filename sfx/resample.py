"""Re-exports core.resample under the name the SFX driver uses.

The resampler moved to core/ when the FM sample pipeline started using it too
(it used to be a box filter there); nothing in sfx/ changed.
"""

from core.resample import DEFAULT_BETA, DEFAULT_PHASES, DEFAULT_TAPS, build_kernel, resample, resample_stereo

__all__ = ["DEFAULT_BETA", "DEFAULT_PHASES", "DEFAULT_TAPS", "build_kernel", "resample",
           "resample_stereo"]
