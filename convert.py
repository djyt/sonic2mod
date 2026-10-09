#!/usr/bin/env python3
"""CLI entry point for SMPS-to-MOD conversion.

Usage:
    python convert.py configs/song.yaml [--output output/song.mod]
    python convert.py configs/moonwalker/81_smooth_criminal.yaml --show-config   # a minimal config, completed
    python convert.py configs/02_green_hill_zone.yaml --variant lofi --merged    # its `variants: lofi` blocks applied
"""

import argparse
import dataclasses
import os
import sys
from pathlib import Path

import yaml

from core.config import (
    PLAYERS,
    ConversionConfig,
    bpm_rounding_options,
    derive_bpm,
    exact_bpm,
    find_settings,
    load_settings,
    with_song_overrides,
)
from core.convert import SampleGenerators, SmpsToModConverter
from core.merge import prepare_merged_config
from core.plan import complete_config
from core.ui import Report, add_variant_argument, branding, cli_console, error_printer, print_report
from sn76489.sample_generator import generate_psg_samples
from ym2612.sample_generator import generate_fm_drums, generate_fm_samples

console = cli_console()


from core.version import get_version as _get_version


def _tag_mod_branding(mod, version: str) -> None:
    labels = [f"SONIC2MOD {version}", "reassembler"]
    label_idx = 0
    for sample in mod.samples:
        if sample.length == 0 and label_idx < len(labels):
            sample.set_name(labels[label_idx])
            label_idx += 1
        if label_idx == len(labels):
            break


_error = error_printer(console)


def _settings_path(config: str, stated: str | None) -> str | None:
    """The global settings file: `--settings` when given (it must exist), else core.config.find_settings."""
    if stated:
        if not os.path.exists(stated):
            _error(f"settings file not found: {stated}")
        return stated
    return find_settings(config)


def main():
    version = _get_version()
    branding(console, "SONIC2MOD", version)

    parser = argparse.ArgumentParser(
        description="Convert Sonic 1 SMPS assembly music to Amiga MOD format"
    )
    parser.add_argument('config', nargs='?', help="YAML configuration file")
    parser.add_argument('--output', '-o', help="Output MOD file path — overrides config output_file")
    parser.add_argument('--input', metavar='FILE',
                        help="Convert FILE instead of the config's input_file (a ROM: with --rom-song)")
    parser.add_argument('--rom-song', metavar='ID',
                        help="With a ROM input: the sound to convert ($81, 0x81)")
    parser.add_argument('--show-config', action='store_true',
                        help="Print the config as converted (a minimal one: with what was derived)")
    parser.add_argument('--write-config', metavar='PATH',
                        help="Write the config as converted to PATH (freeze a derived one to hand-tune) and stop")
    parser.add_argument('--merged', action='store_true',
                        help="The reduced build: fold the config's `merge:` followers onto their "
                             "primaries (composite instruments) and write merge_output_file")
    add_variant_argument(parser)
    parser.add_argument('--settings', metavar='PATH',
                        help="Global settings file (default: settings.yaml beside the config, "
                             "else configs/settings.yaml)")
    parser.add_argument('--player', choices=PLAYERS,
                        help="Tracker the MOD is made for (vibrato depths) — overrides settings.yaml player")
    parser.add_argument('--verbose', '-v', action='store_true',
                        help="Also list every composite, bank sound, loop extension and synthesis "
                             "pitch, and each sample's release rate and share of the song")
    parser.add_argument('--version', action='version',
                        version=f"sonic2mod {_get_version()}")

    args = parser.parse_args()

    if not args.config:
        parser.print_help()
        sys.exit(1)

    try:
        config = ConversionConfig.from_yaml(args.config, args.variant)
    except (ValueError, TypeError) as e:
        _error(str(e))
    if args.input or args.rom_song:
        try:
            config.use_source(args.input or config.input_file, args.rom_song)
        except ValueError as e:
            _error(str(e))

    if not config.input_file:
        _error("No input_file specified in YAML config")

    if not os.path.exists(config.input_file):
        _error(f"Input file not found: {config.input_file}")

    # ── Parse (an asm), read (a ROM) or lift (a VGM rip) ───────────────────────
    try:
        song = config.read_song()
    except ValueError as e:
        _error(str(e))

    # ── A minimal config (no channels:) completed from the song ────────────────
    derived: list[str] = []
    stale: list[str] = []
    data = config.stated()
    if config.is_minimal:
        try:
            settings = load_settings(_settings_path(args.config, args.settings))[0]
            config, derivation = complete_config(config, args.config, settings, song)
        except ValueError as e:
            _error(str(e))
        assert derivation is not None
        data, derived, stale = derivation.data, derivation.derived, derivation.stale
    if args.show_config:
        console.print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None), markup=False, highlight=False)
    if args.write_config:
        Path(args.write_config).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None), encoding="utf-8")
        console.print(f"wrote {args.write_config}")
        return

    if args.merged:
        try:
            prepare_merged_config(config, song)
        except ValueError as e:
            _error(str(e))
    if args.output:
        config.output_file = args.output

    if not args.output and (not config.output_file or config.output_file == "output.mod"):
        base = os.path.splitext(os.path.basename(config.input_file))[0]
        config.output_file = base.replace(" ", "_") + ".mod"


    # BPM derivation.  A MOD BPM is a whole number: the report says how far off the driver's
    # tempo that leaves the song, and which target_speed would leave it closer.
    bpm: dict = {}
    if config.auto_bpm:
        fps = config.fps
        config.target_bpm = derive_bpm(song.header.tempo_divider, song.header.tempo_modifier,
                                       config.ticks_per_row, config.target_speed, fps)
        exact = exact_bpm(song.header.tempo_divider, song.header.tempo_modifier,
                          config.ticks_per_row, config.target_speed, fps)
        if exact == exact:
            err = (config.target_bpm / exact - 1) * 100
            better = [o for o in bpm_rounding_options(song.header.tempo_divider, song.header.tempo_modifier,
                                                      config.ticks_per_row, fps)
                      if abs(o["error_pct"]) < abs(err) - 0.05]
            bpm = {'exact': exact, 'error_pct': err, 'better': better[0] if better else None}

    # ── Convert ───────────────────────────────────────────────────────────────
    SETTINGS_FILE = _settings_path(args.config, args.settings)
    synth, psg_synth = load_settings(SETTINGS_FILE)
    synth, psg_synth = with_song_overrides(synth, config), with_song_overrides(psg_synth, config)
    if args.player:
        synth = dataclasses.replace(synth, player=args.player)

    # The chips render core's samples: core cannot import them, so they are handed in here
    generators = SampleGenerators(fm=generate_fm_samples, psg=generate_psg_samples, fm_drums=generate_fm_drums)
    converter = SmpsToModConverter(song, config, synth=synth, psg_synth=psg_synth, generators=generators)
    with console.status("[dim]Converting…[/dim]", spinner="dots"):
        mod = converter.convert()

    # ── Branding in sample slots ──────────────────────────────────────────────
    _tag_mod_branding(mod, version)

    # ── Write output ──────────────────────────────────────────────────────────
    output_dir = os.path.dirname(config.output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    output_bytes = mod.get_bytes()
    with open(config.output_file, 'wb') as f:
        f.write(output_bytes)

    print_report(console, Report(
        config=config, song=converter.song, converter=converter, output_path=config.output_file,
        output_bytes=len(output_bytes), merged=bool(config.merge_active), verbose=args.verbose,
        synth=synth, psg_synth=psg_synth, bpm=bpm, mod_channels=mod.CHANNELS, patterns=len(mod.patterns),
        derived=derived, stale=stale))


if __name__ == '__main__':
    main()
