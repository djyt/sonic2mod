"""A song analysed for analyze.py: what each channel plays, and the config it suggests.

    song.py       analyze_song: SongAnalysis, a ChannelAnalysis per channel (notes, voices, effects)
    skeleton.py   yaml_skeleton: the suggested config, as text to fill in
"""

from .skeleton import yaml_skeleton
from .song import (
    DAC_NATIVE_INFO,
    DAC_SAMPLE_GROUPS,
    PARTIAL_EFFECTS,
    UNSUPPORTED_EFFECTS,
    ChannelAnalysis,
    PsgToneStats,
    SongAnalysis,
    analyze_song,
    semitone_to_note_name,
    suggest_transpose,
)

__all__ = [
    "DAC_NATIVE_INFO",
    "DAC_SAMPLE_GROUPS",
    "PARTIAL_EFFECTS",
    "UNSUPPORTED_EFFECTS",
    "ChannelAnalysis",
    "PsgToneStats",
    "SongAnalysis",
    "analyze_song",
    "semitone_to_note_name",
    "suggest_transpose",
    "yaml_skeleton",
]
