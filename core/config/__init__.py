"""Per-song conversion configuration and the global synthesis settings.

    configs/<song>.yaml  ->  song.ConversionConfig      (entries.py: its maps and their parsers)
    configs/settings.yaml ->  settings.SynthesisSettings, settings.PsgSynthesisSettings
                              (both a settings.SampleSettings: the `samples:` block they share)
    loader.py: YAML with no key given twice; bpm.py: the MOD BPM of a song's tempo header
"""

from .bpm import bpm_rounding_options, derive_bpm, exact_bpm
from .entries import (
    ChannelConfig,
    DacSampleConfig,
    InstrumentRange,
    MergeGroup,
    PsgInstrumentEntry,
    format_patterns,
    parse_patterns,
    rate3_synth_root_issues,
)
from .loader import load_yaml
from .settings import (
    DEFAULT_AMIGA_CLOCK,
    DEFAULT_PSG_OVERSAMPLE,
    DEFAULT_SHELF_HZ,
    LEGATO_MODES,
    PLAYERS,
    SAMPLE_KEYS,
    SUSTAIN_LOOP_MODES,
    PsgSynthesisSettings,
    SampleSettings,
    SynthesisSettings,
    with_song_overrides,
)
from .song import ConversionConfig

__all__ = [
    "DEFAULT_AMIGA_CLOCK", "DEFAULT_PSG_OVERSAMPLE", "DEFAULT_SHELF_HZ", "LEGATO_MODES", "PLAYERS", "SAMPLE_KEYS",
    "SUSTAIN_LOOP_MODES", "ChannelConfig", "ConversionConfig", "DacSampleConfig", "InstrumentRange", "MergeGroup",
    "PsgInstrumentEntry", "PsgSynthesisSettings", "SampleSettings", "SynthesisSettings", "bpm_rounding_options",
    "derive_bpm", "exact_bpm", "format_patterns", "load_yaml", "parse_patterns", "rate3_synth_root_issues",
    "with_song_overrides",
]
