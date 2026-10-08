"""Per-song conversion configuration and the global synthesis settings.

    configs/<song>.yaml  ->  song.ConversionConfig      (entries.py: its maps and their parsers)
    configs/settings.yaml ->  settings.SynthesisSettings, settings.PsgSynthesisSettings
                              (both a settings.SampleSettings: the `samples:` block they share)
    loader.py: YAML with no key given twice, `variants:` blocks applied
    bpm.py: the MOD BPM of a song's tempo header
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
from .loader import apply_variant, load_yaml, parse_number
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
    find_settings,
    load_settings,
    with_song_overrides,
)
from .song import REGION_FPS, ConversionConfig, region_fps, variant_output_file

__all__ = [
    "DEFAULT_AMIGA_CLOCK", "DEFAULT_PSG_OVERSAMPLE", "DEFAULT_SHELF_HZ", "LEGATO_MODES", "PLAYERS", "REGION_FPS", "SAMPLE_KEYS",
    "SUSTAIN_LOOP_MODES", "ChannelConfig", "ConversionConfig", "DacSampleConfig", "InstrumentRange", "MergeGroup",
    "PsgInstrumentEntry", "PsgSynthesisSettings", "SampleSettings", "SynthesisSettings", "apply_variant",
    "bpm_rounding_options", "derive_bpm", "exact_bpm", "find_settings", "format_patterns", "load_settings", "load_yaml", "parse_number", "parse_patterns",
    "rate3_synth_root_issues", "region_fps", "variant_output_file", "with_song_overrides"
]
