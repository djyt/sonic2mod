#!/usr/bin/env python3
"""CLI entry point for SMPS-to-MOD conversion.

Usage:
    python convert.py <input.asm> [--config config.yaml] [--output output.mod]
    python convert.py <input.asm> --bpm 150 --speed 6 --ticks-per-row 6 --channels 10
"""

import argparse
import os
import sys

from smps_parser import SmpsParser
from smps2mod import SmpsToModConverter
from config import ConversionConfig, derive_bpm, SynthesisSettings


def main():
    parser = argparse.ArgumentParser(
        description="Convert Sonic 1 SMPS assembly music to Amiga MOD format"
    )
    parser.add_argument('input', help="Input SMPS assembly file (.asm)")
    parser.add_argument('--config', '-c', help="YAML configuration file")
    parser.add_argument('--output', '-o', help="Output MOD file path")
    parser.add_argument('--bpm', type=int, default=150, help="Target BPM (32-255)")
    parser.add_argument('--speed', type=int, default=6, help="Target speed / ticks per row (1-31)")
    parser.add_argument('--ticks-per-row', type=float, default=6.0,
                        help="SMPS ticks per MOD row")
    parser.add_argument('--channels', type=int, default=10,
                        help="Number of MOD channels (4/8/10/12/14/16)")
    parser.add_argument('--transpose', type=int, default=None,
                        help="Global transpose override for FM channels")
    parser.add_argument('--name', help="Song name for MOD file")
    parser.add_argument('--auto-bpm', action='store_true',
                        help="Derive BPM from SMPS tempo header (overrides --bpm)")
    parser.add_argument('--region', choices=['ntsc', 'pal'], default='ntsc',
                        help="Console region for BPM derivation (default: ntsc)")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    # Load or create config
    if args.config:
        config = ConversionConfig.from_yaml(args.config)
        # Override input/output from CLI if specified
        if args.input:
            config.input_file = args.input
        if args.output:
            config.output_file = args.output
    else:
        # Create default config from CLI args
        song_name = args.name or os.path.splitext(os.path.basename(args.input))[0]
        config = ConversionConfig.default_sonic1(song_name)
        config.input_file = args.input
        config.target_bpm = args.bpm
        config.target_speed = args.speed
        config.ticks_per_row = args.ticks_per_row
        config.num_mod_channels = args.channels

        if args.transpose is not None:
            for ch in config.channels:
                if ch.source.startswith("FM"):
                    ch.transpose = args.transpose

        if args.auto_bpm:
            config.auto_bpm = True
            config.region = args.region

    # Determine output path
    if args.output:
        config.output_file = args.output
    elif not config.output_file or config.output_file == "output.mod":
        base = os.path.splitext(os.path.basename(args.input))[0]
        config.output_file = base.replace(" ", "_") + ".mod"

    # Parse SMPS assembly
    print(f"Parsing: {config.input_file}")
    smps_parser = SmpsParser()
    song = smps_parser.parse_file(config.input_file)

    # Print parsing summary
    print(f"  Voice bank: {song.header.voice_label}")
    print(f"  FM channels: {song.header.fm_count}, PSG channels: {song.header.psg_count}")
    print(f"  Tempo: divider={song.header.tempo_divider}, modifier={song.header.tempo_modifier}")
    print(f"  Parsed {len(song.channels)} channels, {len(song.voices)} voices")

    cfg_by_source = {ch_cfg.source: ch_cfg for ch_cfg in config.channels}
    dac_idx = fm_idx = psg_idx = 0

    for i, ch in enumerate(song.channels):
        note_count = sum(1 for e in ch.events if e.is_note)
        effect_count = sum(1 for e in ch.events if e.is_effect)
        total_ticks = 0
        if ch.events:
            last = ch.events[-1]
            total_ticks = last.tick_position
            if last.is_note and last.note:
                total_ticks += last.note.duration

        ch_type = ch.header.channel_type
        if ch_type == "DAC":
            source_name = "DAC"
            dac_idx += 1
        elif ch_type == "FM":
            fm_idx += 1
            source_name = f"FM{fm_idx}"
        else:
            psg_idx += 1
            source_name = f"PSG{psg_idx}"

        ch_cfg = cfg_by_source.get(source_name)
        transpose_info = f", transpose={ch_cfg.transpose:+d}" if ch_cfg else ""

        jump_info = f" → jump to {ch.jump_target_label}" if ch.has_jump else ""
        print(f"  [{ch_type}] {ch.header.label}: "
              f"{note_count} notes, {effect_count} effects, {total_ticks} ticks{transpose_info}{jump_info}")

    # Derive BPM from SMPS tempo if requested
    if config.auto_bpm:
        fps = 60 if config.region == "ntsc" else 50
        derived = derive_bpm(
            song.header.tempo_divider,
            song.header.tempo_modifier,
            config.ticks_per_row,
            config.target_speed,
            fps
        )
        print(f"  Auto BPM: divider={song.header.tempo_divider}, "
              f"modifier={song.header.tempo_modifier}, "
              f"region={config.region} ({fps} Hz) -> {derived} BPM")
        config.target_bpm = derived

    # Load synthesis settings
    SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "configs", "settings.yaml")
    synth = SynthesisSettings.from_yaml(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else SynthesisSettings()

    # Convert to MOD
    print(f"\nConverting to MOD format...")
    print(f"  BPM: {config.target_bpm}, Speed: {config.target_speed}, "
          f"Ticks/row: {config.ticks_per_row}, Channels: {config.num_mod_channels}")
    print(f"  Synthesis: {'enabled (' + synth.mode + ')' if synth.enabled else 'disabled'}")

    converter = SmpsToModConverter(song, config, synth=synth)
    mod = converter.convert()

    # Write output
    output_dir = os.path.dirname(config.output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    output_bytes = mod.get_bytes()
    with open(config.output_file, 'wb') as f:
        f.write(output_bytes)

    print(f"\nOutput: {config.output_file}")
    print(f"  Size: {len(output_bytes)} bytes")
    print(f"  Patterns: {len(mod.patterns)}")
    print(f"  Positions: {mod.positions}")


if __name__ == '__main__':
    main()
