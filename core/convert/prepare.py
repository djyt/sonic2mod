"""A conversion set up: what a config goes through before SmpsToModConverter runs on it.  convert.py
uses it, and so does every tool that walks a song as the converter does (survey_context: merge_survey,
fold_csv; vgm_compare, vgm_pitch_audit), so their seconds, BPM and settings are the converter's.

    the config as read
      └► completed from its song (a minimal config: core.plan.complete_config)
          └► the merged build's columns (merged: core.merge.prepare_merged_config)
              └► target_bpm from the song's tempo header (auto_bpm)
                  └► the settings, with the song's own overrides (and the player asked for)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    BpmReport,
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    bpm_report,
    derive_bpm,
    find_settings,
    load_settings,
    with_song_overrides,
)
from ..merge import prepare_merged_config
from ..plan import Derivation, complete_config
from ..smps import SmpsSong, update_hz


@dataclass
class PreparedConversion:
    """A config ready to convert, with its song and the settings it converts under."""
    config: ConversionConfig
    song: SmpsSong
    synth: SynthesisSettings
    psg_synth: PsgSynthesisSettings
    derivation: Derivation | None = None     # a minimal config's: what was derived from the song
    bpm: BpmReport | None = None             # auto_bpm: how far the whole-number BPM is from the tempo


def prepare_conversion(config: ConversionConfig, config_path: str | Path, settings_path: str | None = None, *,
                       song: SmpsSong | None = None, merged: bool = False,
                       player: str | None = None) -> PreparedConversion:
    """`config` (as ConversionConfig.from_yaml reads it, `config_path` its file) set up to convert:
    under `settings_path` (default: core.config.find_settings), the merged build when `merged`,
    `player` (the tracker it is for) over the settings' player and merged_player.  `song`: the
    config's song when already read.  A bad config or song raises ValueError."""
    song = song or config.read_song()
    synth, psg_synth = load_settings(settings_path or find_settings(str(config_path)))
    config, derivation = complete_config(config, config_path, synth, song)

    if merged:
        prepare_merged_config(config, song)
    bpm = _apply_auto_bpm(config, song)

    synth, psg_synth = with_song_overrides(synth, config), with_song_overrides(psg_synth, config)
    if player:
        synth = dataclasses.replace(synth, player=player, merged_player=player)
    return PreparedConversion(config, song, synth, psg_synth, derivation, bpm)


def _apply_auto_bpm(config: ConversionConfig, song: SmpsSong) -> BpmReport | None:
    """auto_bpm: target_bpm from the song's tempo header (a MOD BPM is whole: the report says how
    far that leaves it from the driver's tempo).  None without auto_bpm."""
    if not config.auto_bpm:
        return None
    header = song.header
    fps = update_hz(song.rules, config.region)
    config.target_bpm = derive_bpm(header.tempo_divider, header.tempo_modifier, config.ticks_per_row,
                                   config.target_speed, fps)
    return bpm_report(config.target_bpm, header.tempo_divider, header.tempo_modifier, config.ticks_per_row,
                      config.target_speed, fps)
