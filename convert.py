#!/usr/bin/env python3
"""CLI entry point for SMPS-to-MOD conversion.

Usage:
    python convert.py --config configs/song.yaml [--output output/song.mod]
    python convert.py path/to/song.asm              # quick no-config run
"""

import argparse
import os
import sys

from core.smps_parser import SmpsParser
from core.smps2mod import SmpsToModConverter
from core.config import ConversionConfig, derive_bpm, SynthesisSettings


def main():
    parser = argparse.ArgumentParser(
        description="Convert Sonic 1 SMPS assembly music to Amiga MOD format"
    )
    parser.add_argument('input', nargs='?',
                        help="Input SMPS assembly file (.asm) — overrides config input_file")
    parser.add_argument('--config', '-c', help="YAML configuration file")
    parser.add_argument('--output', '-o', help="Output MOD file path — overrides config output_file")

    args = parser.parse_args()

    # Determine config / input_file
    if args.config:
        config = ConversionConfig.from_yaml(args.config)
        if args.input:
            config.input_file = args.input
        if args.output:
            config.output_file = args.output
    elif args.input:
        song_name = os.path.splitext(os.path.basename(args.input))[0]
        config = ConversionConfig.default_sonic1(song_name)
        config.input_file = args.input
        if args.output:
            config.output_file = args.output
    else:
        parser.print_help()
        sys.exit(1)

    if not config.input_file:
        print("Error: No input file specified (use positional arg or set input_file in YAML)")
        sys.exit(1)

    if not os.path.exists(config.input_file):
        print(f"Error: Input file not found: {config.input_file}")
        sys.exit(1)

    # Default output path if still generic
    if not args.output and (not config.output_file or config.output_file == "output.mod"):
        base = os.path.splitext(os.path.basename(config.input_file))[0]
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

        jump_info = f" -> jump to {ch.jump_target_label}" if ch.has_jump else ""
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
