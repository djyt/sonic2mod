#!/usr/bin/env python3
"""CLI entry point for SMPS-to-MOD conversion.

Usage:
    python convert.py --config configs/song.yaml [--output output/song.mod]
    python convert.py path/to/song.asm              # quick no-config run
"""

import argparse
import io
import os
import sys

# Force UTF-8 output on Windows so Rich can render Unicode symbols
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table

from core.config import ConversionConfig, PsgSynthesisSettings, SynthesisSettings, derive_bpm
from core.mod import apply_pattern_breaks
from core.smps2mod import SmpsToModConverter
from core.smps_parser import SmpsParser

console = Console(highlight=False, legacy_windows=False)

_LABEL_W = 9   # right-aligned label column width
_INDENT  = " " * (_LABEL_W + 4)  # indentation for continuation lines


def _row(label: str, *lines: str):
    """Print a labelled section row with aligned continuation lines."""
    for i, line in enumerate(lines):
        if not line:
            continue
        if i == 0:
            console.print(f"  [bold]{label:>{_LABEL_W}}[/bold]   {line}")
        else:
            console.print(f"  {' ' * _LABEL_W}   {line}")


def _error(msg: str):
    console.print(f"\n  [bold red]{'Error':>{_LABEL_W}}[/bold red]   {msg}\n")
    sys.exit(1)


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
        _error("No input file specified (use positional arg or set input_file in YAML)")

    if not os.path.exists(config.input_file):
        _error(f"Input file not found: {config.input_file}")

    if not args.output and (not config.output_file or config.output_file == "output.mod"):
        base = os.path.splitext(os.path.basename(config.input_file))[0]
        config.output_file = base.replace(" ", "_") + ".mod"

    # ── Header ──────────────────────────────────────────────────────────────
    console.print()
    console.rule(f"[bold cyan]sonic2mod[/bold cyan]   [dim]{config.name}[/dim]")
    console.print()

    # ── Parse ────────────────────────────────────────────────────────────────
    smps_parser = SmpsParser()
    song = smps_parser.parse_file(config.input_file)

    fm_count  = sum(1 for ch in song.channels if ch.header.channel_type == "FM")
    psg_count = sum(1 for ch in song.channels if ch.header.channel_type == "PSG")
    dac_count = sum(1 for ch in song.channels if ch.header.channel_type == "DAC")

    ch_parts = []
    if dac_count:  ch_parts.append("DAC")
    if fm_count:   ch_parts.append(f"{fm_count} FM")
    if psg_count:  ch_parts.append(f"{psg_count} PSG")

    # BPM derivation (before the Convert header so we can show it in Parse)
    bpm_note = ""
    if config.auto_bpm:
        fps = 60 if config.region == "ntsc" else 50
        derived = derive_bpm(
            song.header.tempo_divider,
            song.header.tempo_modifier,
            config.ticks_per_row,
            config.target_speed,
            fps,
        )
        config.target_bpm = derived
        bpm_note = "  [dim](auto-derived)[/dim]"

    _row("Parse",
         f"[cyan]{config.input_file}[/cyan]",
         f"Voice bank [bold]{song.header.voice_label}[/bold]  ·  "
         + "  ·  ".join(ch_parts),
         f"Tempo  div={song.header.tempo_divider}  mod={song.header.tempo_modifier}  ·  "
         f"{config.region.upper()}  ·  [bold]{config.target_bpm}[/bold] BPM{bpm_note}",
    )

    # ── Channel table ────────────────────────────────────────────────────────
    cfg_by_source = {ch_cfg.source: ch_cfg for ch_cfg in config.channels}
    tbl = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        show_header=True,
        header_style="bold dim",
        padding=(0, 2, 0, 0),
    )
    tbl.add_column("Channel",   style="bold cyan", no_wrap=True)
    tbl.add_column("Notes",     justify="right")
    tbl.add_column("Effects",   justify="right")
    tbl.add_column("Ticks",     justify="right", style="dim")
    tbl.add_column("Transpose", justify="right", style="dim")
    tbl.add_column("Jump",      style="dim")

    dac_idx = fm_idx = psg_idx = 0
    for ch in song.channels:
        note_count   = sum(1 for e in ch.events if e.is_note and not e.note.is_rest)
        effect_count = sum(1 for e in ch.events if e.is_effect)
        total_ticks  = 0
        if ch.events:
            last = ch.events[-1]
            total_ticks = last.tick_position + (last.note.duration if last.is_note and last.note else 0)

        ch_type = ch.header.channel_type
        if ch_type == "DAC":
            dac_idx += 1;  source_name = "DAC"
        elif ch_type == "FM":
            fm_idx += 1;   source_name = f"FM{fm_idx}"
        else:
            psg_idx += 1;  source_name = f"PSG{psg_idx}"

        ch_cfg   = cfg_by_source.get(source_name)
        tr_val   = (ch_cfg.transpose if ch_cfg else 0)
        tr_str   = f"{tr_val:+d}" if tr_val != 0 else ""
        jump_str = f"-> {ch.jump_target_label}" if ch.has_jump else ""

        tbl.add_row(source_name, str(note_count), str(effect_count),
                    str(total_ticks), tr_str, jump_str)

    console.print()
    console.print(Padding(tbl, (0, 0, 0, _LABEL_W + 4)))

    # ── Convert ───────────────────────────────────────────────────────────────
    SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "configs", "settings.yaml")
    synth     = SynthesisSettings.from_yaml(SETTINGS_FILE)     if os.path.exists(SETTINGS_FILE) else SynthesisSettings()
    psg_synth = PsgSynthesisSettings.from_yaml(SETTINGS_FILE)  if os.path.exists(SETTINGS_FILE) else PsgSynthesisSettings()

    fm_synth_str  = (f"[green]enabled[/green] [dim]({synth.mode})[/dim]"
                     if synth.enabled else "[dim]disabled[/dim]")
    psg_synth_str = ("[green]enabled[/green]"
                     if psg_synth.enabled else "[dim]disabled[/dim]")

    console.print()
    _row("Convert",
         f"BPM [bold]{config.target_bpm}[/bold]  ·  "
         f"Speed [bold]{config.target_speed}[/bold]  ·  "
         f"Ticks/row [bold]{config.ticks_per_row}[/bold]  ·  "
         f"[bold]{config.num_mod_channels}[/bold] channels",
         f"FM synthesis {fm_synth_str}  ·  PSG synthesis {psg_synth_str}",
    )

    converter = SmpsToModConverter(song, config, synth=synth, psg_synth=psg_synth)
    needs_synthesis = (synth.enabled or psg_synth.enabled)
    if needs_synthesis:
        with console.status("[dim]Synthesizing samples…[/dim]", spinner="dots"):
            mod = converter.convert()
    else:
        mod = converter.convert()

    # ── Pattern breaks ────────────────────────────────────────────────────────
    if config.mod_pattern_breaks:
        apply_pattern_breaks(mod, config.mod_pattern_breaks)

    # ── Loop point (post-break so positions reflect final layout) ─────────────
    converter._set_loop_point(config.mod_pattern_breaks or [])

    # ── Write output ──────────────────────────────────────────────────────────
    output_dir = os.path.dirname(config.output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    output_bytes = mod.get_bytes()
    with open(config.output_file, 'wb') as f:
        f.write(output_bytes)

    # Build detail lines from converter._infos
    loop_target = None
    detail_lines = []
    for info in converter._infos:
        if info['type'] == 'loop_set':
            loop_target = info['target']
        elif info['type'] == 'loop_extended':
            detail_lines.append(
                f"[dim]{info['label']}[/dim]  loop extended "
                f"{info['from']} → [bold]{info['to']}[/bold] events"
            )
        elif info['type'] == 'fm_synthesized':
            detail_lines.append(
                f"FM synthesized [bold]{info['count']}[/bold] instrument"
                f"{'s' if info['count'] != 1 else ''}"
            )
        elif info['type'] == 'psg_synthesized':
            detail_lines.append(
                f"PSG synthesized [bold]{info['count']}[/bold] instrument"
                f"{'s' if info['count'] != 1 else ''}"
            )

    loop_str = (f"  ·  loop [dim]→[/dim] pattern [bold]{loop_target}[/bold]"
                if loop_target is not None else "")

    console.print()
    _row("Output",
         f"[cyan]{config.output_file}[/cyan]",
         f"{len(output_bytes):,} bytes  ·  {len(mod.patterns)} patterns{loop_str}",
         *detail_lines,
    )

    # ── Warnings ──────────────────────────────────────────────────────────────
    warnings = converter._warnings
    if warnings:
        console.print()
        console.rule(
            f"[bold yellow]{len(warnings)} warning{'s' if len(warnings) > 1 else ''}[/bold yellow]",
            style="yellow dim",
        )
        for w in warnings:
            _render_warning(w)

    console.print()


def _render_warning(w: dict):
    """Render a single structured warning."""
    wtype   = w['type']
    channel = w.get('channel', '')
    ctx     = w.get('extra_ctx')

    # Context suffix: PSG voice label or FM voice index
    if ctx:
        ctx_str = f"  [dim]{ctx}[/dim]"
    elif w.get('voice_idx') is not None:
        ctx_str = f"  [dim]voice {w['voice_idx']}[/dim]"
    else:
        ctx_str = ""

    if wtype in ('clamp_high', 'clamp_low'):
        direction = "above" if wtype == 'clamp_high' else "below"
        clamped   = "B3" if wtype == 'clamp_high' else "C1"
        map_key   = "low" if wtype == 'clamp_high' else "high"
        src       = w['src_name']
        nv        = w['note_value']
        tr        = w['transpose']
        boundary  = w['boundary']
        semitone  = (nv - 0x81) + tr

        console.print(
            f"\n  [bold yellow]![/bold yellow]  "
            f"[bold]{channel}[/bold]{ctx_str}  "
            f"[yellow]n{src} clamped to {clamped}[/yellow]"
        )
        console.print(
            f"     [dim]n{src} ({nv:#04x}) + transpose {tr:+d} "
            f"→ semitone {semitone}, outside MOD range 0–35[/dim]"
        )
        console.print(
            f"     Notes {direction} [bold]{boundary}[/bold] will clamp "
            f"at this transpose."
        )
        ctx = w.get('extra_ctx', '')
        avail = w.get('psg_available_labels')
        if ctx and not ctx.startswith('form '):
            map_ref = f"psg_voice_map entry [bold]{ctx}[/bold]"
        elif ctx and ctx.startswith('form '):
            map_ref = f"psg_map entry [bold]{ctx}[/bold]"
        elif channel.startswith('PSG'):
            map_ref = "psg_voice_map or psg_map entry"
        else:
            map_ref = "voice_map entry"
        console.print(
            f"     [green]Fix:[/green] add a {map_ref} with  "
            f"[bold cyan]{map_key}: {src}[/bold cyan]"
        )
        if avail:
            labels = "  ".join(f"[bold]{lb}[/bold]" for lb in avail)
            console.print("     [dim]No psg_voice_map entry was active when this note fired.[/dim]")
            console.print(f"     [dim]Check these entries: {labels}[/dim]")

    elif wtype == 'map_gap':
        note  = w['note_name']
        vidx  = w.get('voice_idx')
        rlo   = w['range_lo']
        rhi   = w['range_hi']
        sem   = w['semitone']

        console.print(
            f"\n  [bold yellow]![/bold yellow]  "
            f"[bold]{channel}[/bold]  [dim]voice {vidx}[/dim]  "
            f"[yellow]n{note} not covered by voice_map[/yellow]"
        )
        console.print(
            f"     [dim]semitone {sem} is outside all mapped ranges  "
            f"(config spans {rlo}–{rhi})[/dim]"
        )
        console.print(
            f"     [green]Fix:[/green] extend the voice_map entry's  "
            f"[bold cyan]high: {note}[/bold cyan]  or add a new range."
        )

    elif wtype == 'missing_source':
        src = w['source']
        console.print(
            f"\n  [bold yellow]![/bold yellow]  "
            f"[yellow]Source [bold]{src}[/bold] not found in parsed song[/yellow]"
        )
        console.print(
            "     [green]Fix:[/green] check the [cyan]source:[/cyan] field in your channel config."
        )

    elif wtype == 'pattern_overflow':
        pat = w['pattern']
        mx  = w['max']
        console.print(
            f"\n  [bold yellow]![/bold yellow]  "
            f"[bold]{channel}[/bold]  "
            f"[yellow]pattern {pat} exceeds max_patterns ({mx}) — channel truncated[/yellow]"
        )
        console.print(
            "     [green]Fix:[/green] increase [cyan]max_patterns:[/cyan] in your YAML config."
        )


if __name__ == '__main__':
    main()
