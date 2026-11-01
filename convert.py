#!/usr/bin/env python3
"""CLI entry point for SMPS-to-MOD conversion.

Usage:
    python convert.py configs/song.yaml [--output output/song.mod]
    python convert.py configs/moonwalker/81_smooth_criminal.yaml --show-config   # a minimal config, completed
    python convert.py configs/sonic_1/02_green_hill_zone.yaml --variant lofi --merged    # its `variants: lofi` blocks applied
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

from core.config import PLAYERS, ConversionConfig, find_settings
from core.convert import SmpsToModConverter, prepare_conversion
from core.ui import Report, add_variant_argument, branding, cli_console, error_printer, print_report

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
                        help="With a ROM input: the sound to convert, by its driver's ID ($81, 0x81; Streets of Rage 2: $00 ...)")
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
                        help="Tracker the MOD is made for (vibrato depths) — overrides settings.yaml player and merged_player")
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

    # ── Read the song (asm parsed, ROM read, VGM lifted) and set the conversion up: a minimal
    #    config completed, the merged build's columns, auto_bpm, the settings ─────────────────
    try:
        prep = prepare_conversion(config, args.config, _settings_path(args.config, args.settings),
                                  merged=args.merged and not args.write_config, player=args.player)
    except ValueError as e:
        _error(str(e))
    config, song, synth, psg_synth = prep.config, prep.song, prep.synth, prep.psg_synth

    # ── A minimal config: what was derived, shown or written ───────────────────
    derivation = prep.derivation
    data = derivation.data if derivation else config.stated()
    if args.show_config:
        console.print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None), markup=False, highlight=False)
    if args.write_config:
        Path(args.write_config).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None), encoding="utf-8")
        console.print(f"wrote {args.write_config}")
        return

    if args.output:
        config.output_file = args.output

    if not args.output and (not config.output_file or config.output_file == "output.mod"):
        base = os.path.splitext(os.path.basename(config.input_file))[0]
        config.output_file = base.replace(" ", "_") + ".mod"

    # ── Convert ───────────────────────────────────────────────────────────────
    # The chips render core's samples: core cannot import them, so they are handed in here
    converter = SmpsToModConverter(song, config, synth=synth, psg_synth=psg_synth)
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
        synth=synth, psg_synth=psg_synth, bpm=prep.bpm, mod_channels=mod.CHANNELS, patterns=len(mod.patterns),
        derived=derivation.derived if derivation else [], stale=derivation.stale if derivation else [],
        folded=derivation.folded if derivation else {},
        volumes_folded=derivation.volumes_folded if derivation else {},
        windows_merged=derivation.windows_merged if derivation else {}))


if __name__ == '__main__':
    main()
