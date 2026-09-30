#!/usr/bin/env python3
"""CLI entry point for SMPS-to-MOD conversion.

Usage:
    python convert.py configs/song.yaml [--output output/song.mod]
"""

import argparse
import os
import sys

from core.cli import branding, cli_console, error_printer
from core.config import (
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    bpm_rounding_options,
    derive_bpm,
    exact_bpm,
)
from core.merge import prepare_merged_config
from core.report import Report, print_report
from core.smps2mod import SmpsToModConverter
from core.smps_parser import SmpsParser

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


def main():
    version = _get_version()
    branding(console, "SONIC2MOD", version)

    parser = argparse.ArgumentParser(
        description="Convert Sonic 1 SMPS assembly music to Amiga MOD format"
    )
    parser.add_argument('config', nargs='?', help="YAML configuration file")
    parser.add_argument('--output', '-o', help="Output MOD file path — overrides config output_file")
    parser.add_argument('--merged', action='store_true',
                        help="The reduced build: fold the config's `merge:` followers onto their "
                             "primaries (composite instruments) and write merge_output_file")
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
        config = ConversionConfig.from_yaml(args.config)
    except (ValueError, TypeError) as e:
        _error(str(e))
    if args.merged:
        try:
            prepare_merged_config(config)
        except ValueError as e:
            _error(str(e))
    if args.output:
        config.output_file = args.output

    if not config.input_file:
        _error("No input_file specified in YAML config")

    if not os.path.exists(config.input_file):
        _error(f"Input file not found: {config.input_file}")

    if not args.output and (not config.output_file or config.output_file == "output.mod"):
        base = os.path.splitext(os.path.basename(config.input_file))[0]
        config.output_file = base.replace(" ", "_") + ".mod"

    # ── Parse ────────────────────────────────────────────────────────────────
    song = SmpsParser().parse_file(config.input_file)

    # BPM derivation.  A MOD BPM is a whole number: the report says how far off the driver's
    # tempo that leaves the song, and which target_speed would leave it closer.
    bpm: dict = {}
    if config.auto_bpm:
        fps = 60 if config.region == "ntsc" else 50
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
    # Resolve settings.yaml relative to the config file (both live in configs/).
    # Falls back to __file__-relative for editable installs / direct invocation.
    _config_dir   = os.path.dirname(os.path.abspath(args.config))
    SETTINGS_FILE = os.path.join(_config_dir, "settings.yaml")
    if not os.path.exists(SETTINGS_FILE):
        SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "settings.yaml")
    synth     = SynthesisSettings.from_yaml(SETTINGS_FILE)     if os.path.exists(SETTINGS_FILE) else SynthesisSettings()
    psg_synth = PsgSynthesisSettings.from_yaml(SETTINGS_FILE)  if os.path.exists(SETTINGS_FILE) else PsgSynthesisSettings()
    if config.loop_drift_db is not None:            # the song's own loop_drift_db over settings.yaml's
        import dataclasses as _dc
        synth = _dc.replace(synth, loop_drift_db=config.loop_drift_db)
        psg_synth = _dc.replace(psg_synth, loop_drift_db=config.loop_drift_db)

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
        config=config, song=song, converter=converter, output_path=config.output_file,
        output_bytes=len(output_bytes), merged=bool(config.merge_active), verbose=args.verbose,
        synth=synth, psg_synth=psg_synth, bpm=bpm, mod_channels=mod.CHANNELS, patterns=len(mod.patterns)))


if __name__ == '__main__':
    main()
