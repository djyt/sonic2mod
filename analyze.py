#!/usr/bin/env python3
"""SMPS song analyser — Rich-formatted analysis of a parsed SMPS file.

Usage:
    python analyze.py song.asm [--config configs/song.yaml] [--region ntsc|pal]

Requires: pip install rich
"""

import argparse
import io
import os
import sys

# Force UTF-8 output on Windows so Rich can render Unicode symbols
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.syntax import Syntax
    from rich import box
except ImportError:
    print("Error: 'rich' is required. Install with: pip install rich")
    sys.exit(1)

from core.smps_parser import SmpsParser
from core.config import ConversionConfig
from core.analysis import (
    analyze_song, SongAnalysis, ChannelAnalysis,
    semitone_to_note_name, suggest_transpose,
    UNSUPPORTED_EFFECTS, PARTIAL_EFFECTS, DAC_NATIVE_INFO,
    _CARRIER_LABELS_BY_ALG,
)

console = Console(legacy_windows=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _note_range_str(ch: ChannelAnalysis) -> str:
    if ch.min_semitone is None:
        return "—"
    lo = semitone_to_note_name(ch.min_semitone)
    hi = semitone_to_note_name(ch.max_semitone)
    return f"{lo}–{hi} (semitones {ch.min_semitone}–{ch.max_semitone})"


def _effect_summary(effect_counts: dict, label: str = "Effects") -> str:
    parts = [f"{et} ×{n}" for et, n in sorted(effect_counts.items())]
    return "  ".join(parts) if parts else "—"


def _unsupported_summary(effect_counts: dict) -> str:
    parts = []
    for et, n in sorted(effect_counts.items()):
        if et in UNSUPPORTED_EFFECTS:
            parts.append(f"{et} ×{n}")
    return "  ".join(parts) if parts else None


def _partial_summary(effect_counts: dict) -> list[str]:
    parts = []
    for et, n in sorted(effect_counts.items()):
        if et in PARTIAL_EFFECTS:
            parts.append(f"{et} ×{n}  ({PARTIAL_EFFECTS[et]})")
    return parts


def _normal_effect_summary(effect_counts: dict) -> str:
    """Effects excluding unsupported/partial ones."""
    parts = []
    for et, n in sorted(effect_counts.items()):
        if et not in UNSUPPORTED_EFFECTS and et not in PARTIAL_EFFECTS:
            parts.append(f"{et} ×{n}")
    return "  ".join(parts) if parts else "—"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def render_header(analysis: SongAnalysis, region: str):
    song = analysis.song
    fname = os.path.basename(analysis.file_path)
    h = song.header

    bpm = analysis.derived_bpm_ntsc if region == "ntsc" else analysis.derived_bpm_pal
    bpm_ntsc = analysis.derived_bpm_ntsc
    bpm_pal = analysis.derived_bpm_pal

    lines = [
        f"[bold cyan]{fname}[/bold cyan]",
        f"Voice bank: [yellow]{h.voice_label}[/yellow]  |  "
        f"FM: [green]{h.fm_count}ch[/green]  |  "
        f"PSG: [yellow]{h.psg_count}ch[/yellow]  |  "
        f"DAC: [cyan]1ch[/cyan]  |  "
        f"Voices: [white]{len(song.voices)}[/white]",
        f"Tempo: divider=${h.tempo_divider:02X}, modifier=${h.tempo_modifier:02X}  →  "
        f"[bold]{bpm_ntsc} BPM (NTSC)[/bold] / {bpm_pal} BPM (PAL)",
    ]
    console.print(Panel("\n".join(lines), title="SMPS Analysis", border_style="bright_blue"))


def render_channel_dac(ch: ChannelAnalysis, config: ConversionConfig | None):
    lines = []

    loop_str = f" | Loop → {ch.loop_target}" if ch.has_loop else ""
    lines.append(
        f"Notes: [bold]{ch.note_count}[/bold]  |  "
        f"Ticks: {ch.total_ticks}{loop_str}"
    )

    # Sample counts
    if ch.dac_counts:
        samples_line = "Samples:  " + "  ".join(
            f"[cyan]{name}[/cyan] ×{count}"
            for name, count in sorted(ch.dac_counts.items(), key=lambda x: -x[1])
        )
        lines.append(samples_line)

    # Effects (excluding ignored DAC-level effects)
    normal_eff = _normal_effect_summary(ch.effect_counts)
    if normal_eff != "—":
        lines.append(f"Effects:  {normal_eff}")

    unsup = _unsupported_summary(ch.effect_counts)
    if unsup:
        lines.append(f"[dim]Unsupported:[/dim] {unsup}")

    # Config coverage
    if config is not None:
        dac_cfgs = {d.name: d for d in config.dac_samples}
        cov_parts = []
        for name in ch.dac_counts:
            if name in dac_cfgs:
                d = dac_cfgs[name]
                cov_parts.append(f"[green]✓[/green] {name} → inst {d.mod_instrument} @ {d.mod_note}")
            else:
                cov_parts.append(f"[red]✗[/red] {name} not configured")
        if ch.config_enabled is False:
            lines.append("[dim]config: disabled[/dim]")
        elif cov_parts:
            lines.append("Coverage:  " + "  ".join(cov_parts))

    console.print(Panel(
        "\n".join(lines),
        title=f"[cyan bold]DAC[/cyan bold]",
        border_style="cyan",
    ))


def render_channel_fm(ch: ChannelAnalysis, config: ConversionConfig | None):
    lines = []

    loop_str = f" | Loop → {ch.loop_target}" if ch.has_loop else ""
    lines.append(
        f"Notes: [bold]{ch.note_count}[/bold]  |  "
        f"Ticks: {ch.total_ticks}{loop_str}"
    )

    range_str = _note_range_str(ch)
    lines.append(f"Note range: {range_str}  [dim](raw SMPS bytes)[/dim]")
    if ch.initial_transpose != 0 and ch.min_semitone is not None:
        eff_lo = semitone_to_note_name(ch.min_semitone + ch.initial_transpose)
        eff_hi = semitone_to_note_name(ch.max_semitone + ch.initial_transpose)
        lines.append(
            f"  [dim]Effective chip pitch (initial transpose {ch.initial_transpose:+d}): "
            f"≈ {eff_lo}–{eff_hi}"
            + (" (varies with smpsChangeTransposition)" if ch.has_transpose_change else "")
            + "[/dim]"
        )

    # Transpose change
    if ch.has_transpose_change:
        events_str = "  ".join(
            f"tick {e.tick}: {e.delta:+d} → cumulative {e.cumulative:+d}"
            for e in ch.transpose_events
        )
        lines.append(
            f"[yellow]smpsChangeTransposition: YES[/yellow]  "
            f"[dim]← do NOT use root on this channel[/dim]"
        )
        lines.append(f"  Events: {events_str}")
    else:
        lines.append("[green]smpsChangeTransposition: NO[/green]  [dim]← root: safe[/dim]")

    # Per-voice stats
    if ch.voice_stats:
        lines.append("Voices used:")
        for vi, vs in sorted(ch.voice_stats.items()):
            if vs.note_count == 0:
                range_str = "—"
            else:
                lo = semitone_to_note_name(vs.min_semitone)
                hi = semitone_to_note_name(vs.max_semitone)
                range_str = f"{lo}–{hi}"
            lines.append(
                f"  [yellow]${vi:02X}[/yellow]  {range_str}  "
                f"({vs.note_count} notes, {vs.switch_count} switches)"
            )

    # Effects
    normal_eff = _normal_effect_summary(ch.effect_counts)
    if normal_eff != "—":
        lines.append(f"Effects: {normal_eff}")

    unsup = _unsupported_summary(ch.effect_counts)
    if unsup:
        lines.append(f"[dim]Unsupported:[/dim] {unsup}")

    for part in _partial_summary(ch.effect_counts):
        lines.append(f"[yellow]Partial:[/yellow] {part}")

    # Config coverage
    if config is not None:
        if ch.config_enabled is False:
            lines.append("[dim]config: disabled[/dim]")
        else:
            cov_parts = []
            for vi, vs in sorted(ch.voice_stats.items()):
                ranges = (
                    config.channel_instrument_map.get(ch.name, {}).get(vi) or
                    config.voice_map.get(vi)
                )
                if ranges:
                    lo = semitone_to_note_name(vs.min_semitone)
                    hi = semitone_to_note_name(vs.max_semitone)
                    root_str = ""
                    for r in ranges:
                        if r.root:
                            root_str = f" root={r.root.name}"
                            break
                    cov_parts.append(
                        f"[green]✓[/green] voice ${vi:02X}{root_str} → inst {ranges[0].mod_instrument}"
                    )
                else:
                    cov_parts.append(f"[red]✗[/red] voice ${vi:02X} not in voice_map")
            if cov_parts:
                lines.append("Config:  " + "  ".join(cov_parts))

            if ch.uncovered_notes:
                unc_str = ", ".join(semitone_to_note_name(s) for s in ch.uncovered_notes[:10])
                if len(ch.uncovered_notes) > 10:
                    unc_str += f" (+{len(ch.uncovered_notes) - 10} more)"
                lines.append(f"[red]Uncovered notes:[/red] {unc_str}")

    console.print(Panel(
        "\n".join(lines),
        title=f"[green bold]{ch.name}[/green bold]",
        border_style="green",
    ))


def render_channel_psg(ch: ChannelAnalysis, config: ConversionConfig | None):
    lines = []

    loop_str = f" | Loop → {ch.loop_target}" if ch.has_loop else ""
    lines.append(
        f"Notes: [bold]{ch.note_count}[/bold]  |  "
        f"Ticks: {ch.total_ticks}{loop_str}"
    )

    if ch.min_semitone is not None:
        lines.append(f"Note range: {_note_range_str(ch)}")
        sug = suggest_transpose(ch.min_semitone, ch.max_semitone)
        lines.append(f"  → suggested transpose: [bold]{sug:+d}[/bold]")

    normal_eff = _normal_effect_summary(ch.effect_counts)
    if normal_eff != "—":
        lines.append(f"Effects: {normal_eff}")

    unsup = _unsupported_summary(ch.effect_counts)
    if unsup:
        lines.append(f"[dim]Unsupported:[/dim] {unsup}")

    for part in _partial_summary(ch.effect_counts):
        lines.append(f"[yellow]Partial:[/yellow] {part}")

    if config is not None:
        if ch.config_enabled is False:
            lines.append("[dim]config: disabled[/dim]")

    console.print(Panel(
        "\n".join(lines),
        title=f"[yellow bold]{ch.name}[/yellow bold]",
        border_style="yellow",
    ))


def render_voices_table(analysis: SongAnalysis):
    """Print the voice table with algorithm, feedback, carriers, and usage."""
    song = analysis.song

    # Build voice usage map: voice_idx -> list of "FM1 (C3-F4)" strings
    voice_usage: dict[int, list[str]] = {}
    for ch_an in analysis.channels:
        if ch_an.channel_type == "FM":
            for vi, vs in ch_an.voice_stats.items():
                if vs.note_count == 0:
                    entry = f"{ch_an.name}"
                else:
                    lo = semitone_to_note_name(vs.min_semitone)
                    hi = semitone_to_note_name(vs.max_semitone)
                    entry = f"{ch_an.name} ({lo}–{hi})"
                voice_usage.setdefault(vi, []).append(entry)

    if not song.voices:
        return

    table = Table(title="FM Voices", box=box.SIMPLE_HEAVY, border_style="bright_blue")
    table.add_column("#", style="yellow", width=5)
    table.add_column("Alg", width=5)
    table.add_column("FB", width=4)
    table.add_column("Carriers", width=16)
    table.add_column("Used by")

    for voice in song.voices:
        alg = voice.algorithm
        carriers = "+".join(_CARRIER_LABELS_BY_ALG.get(alg, ["?"]))
        used_by = ", ".join(voice_usage.get(voice.index, ["—"]))
        table.add_row(
            f"${voice.index:02X}",
            str(alg),
            str(voice.feedback),
            carriers,
            used_by,
        )

    console.print(table)


def render_config_coverage(analysis: SongAnalysis):
    """Print a coverage summary table (only when config is provided)."""
    if analysis.config is None:
        return

    table = Table(title="Config Coverage", box=box.SIMPLE_HEAVY, border_style="bright_blue")
    table.add_column("Channel", width=8)
    table.add_column("Voice", width=7)
    table.add_column("Range", width=14)
    table.add_column("Coverage")

    for ch_an in analysis.channels:
        config = analysis.config

        if ch_an.channel_type == "DAC":
            dac_cfgs = {d.name: d for d in config.dac_samples}
            for name, count in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
                if name in dac_cfgs:
                    d = dac_cfgs[name]
                    native_note, _ = DAC_NATIVE_INFO.get(name, ("?", 0))
                    cov = f"[green]✓[/green] inst {d.mod_instrument}, note {d.mod_note}"
                else:
                    cov = "[red]✗[/red] not configured"
                table.add_row(ch_an.name, name, "—", cov)

        elif ch_an.channel_type == "FM":
            if not ch_an.voice_stats:
                table.add_row(ch_an.name, "—", "—",
                              "[dim]no notes[/dim]" if ch_an.config_enabled else "[dim]disabled[/dim]")
            else:
                for vi, vs in sorted(ch_an.voice_stats.items()):
                    if vs.note_count == 0:
                        rng = "—"
                    else:
                        lo = semitone_to_note_name(vs.min_semitone)
                        hi = semitone_to_note_name(vs.max_semitone)
                        rng = f"{lo}–{hi}"
                    ranges = (
                        config.channel_instrument_map.get(ch_an.name, {}).get(vi) or
                        config.voice_map.get(vi)
                    )
                    if ranges:
                        root_str = ""
                        for r in ranges:
                            if r.root:
                                root_str = f" (root {r.root.name})"
                                break
                        cov = f"[green]✓[/green] mod_instrument {ranges[0].mod_instrument}{root_str}"
                    else:
                        cov = "[red]✗[/red] not in voice_map"
                    table.add_row(ch_an.name, f"${vi:02X}", rng, cov)

        else:  # PSG
            enabled_str = "" if ch_an.config_enabled else "[dim]disabled[/dim]"
            table.add_row(ch_an.name, "—", "—", enabled_str or "[dim]PSG (no voice_map)[/dim]")

    console.print(table)


def render_yaml_skeleton(analysis: SongAnalysis, region: str):
    """Print a suggested YAML skeleton."""
    song = analysis.song
    fname = os.path.basename(analysis.file_path)
    bpm = analysis.derived_bpm_ntsc if region == "ntsc" else analysis.derived_bpm_pal

    lines = [
        f"# Generated by analyze.py — fill in mod_instrument and root values",
        f"# Song: {fname}",
        f"",
        f"name: {os.path.splitext(fname)[0]}",
        f"input_file: {analysis.file_path}",
        f"output_file: output/{os.path.splitext(fname)[0].replace(' ', '_')}.mod",
        f"",
        f"target_bpm: {int(bpm)}",
        f"target_speed: 6",
        f"ticks_per_row: 6",
        f"num_mod_channels: {len(analysis.channels)}",
        f"region: {region}",
        f"",
        f"channels:",
    ]

    mod_ch = 0
    for ch_an in analysis.channels:
        ch_type = ch_an.channel_type
        if ch_type == "DAC":
            lines.append(f"  - source: DAC")
            lines.append(f"    mod_channel: {mod_ch}")
            lines.append(f"    instrument: 1")
            lines.append(f"    enabled: true")
        elif ch_type == "FM":
            sug_trans = -36
            if ch_an.min_semitone is not None:
                sug_trans = suggest_transpose(ch_an.min_semitone, ch_an.max_semitone) - 12
            range_str = ""
            if ch_an.min_semitone is not None:
                lo = semitone_to_note_name(ch_an.min_semitone)
                hi = semitone_to_note_name(ch_an.max_semitone)
                range_str = f"  # note range {lo}–{hi}"
            lines.append(f"  - source: {ch_an.name}")
            lines.append(f"    mod_channel: {mod_ch}")
            lines.append(f"    transpose: {sug_trans}{range_str}")
            lines.append(f"    enabled: true")
        else:  # PSG
            sug_trans = -36
            if ch_an.min_semitone is not None:
                sug_trans = suggest_transpose(ch_an.min_semitone, ch_an.max_semitone) - 12
            lines.append(f"  - source: {ch_an.name}")
            lines.append(f"    mod_channel: {mod_ch}")
            lines.append(f"    transpose: {sug_trans}")
            lines.append(f"    enabled: true")
        mod_ch += 1

    # DAC samples
    dac_channels = [ch for ch in analysis.channels if ch.channel_type == "DAC"]
    if dac_channels:
        lines.append(f"")
        lines.append(f"dac_samples:")
        inst_num = 1
        seen: set[str] = set()
        for ch_an in dac_channels:
            for name, count in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
                if name in seen:
                    continue
                seen.add(name)
                native_note, native_rate = DAC_NATIVE_INFO.get(name, ("C3", 8000))
                lines.append(f"  # {name}: ×{count} — native ~{native_rate} Hz → note {native_note}")
                lines.append(f"  - name: {name}")
                lines.append(f"    mod_instrument: {inst_num}")
                lines.append(f"    mod_note: {native_note}")
                inst_num += 1

    # voice_map
    fm_channels = [ch for ch in analysis.channels if ch.channel_type == "FM"]
    all_voices: dict[int, list[str]] = {}
    for ch_an in fm_channels:
        for vi, vs in ch_an.voice_stats.items():
            all_voices.setdefault(vi, []).append(ch_an.name)

    if all_voices:
        lines.append(f"")
        lines.append(f"voice_map:")
        for vi in sorted(all_voices.keys()):
            # Find voice definition
            voice_def = next((v for v in song.voices if v.index == vi), None)
            alg_str = f"Alg {voice_def.algorithm}, FB {voice_def.feedback}" if voice_def else "unknown"
            used_by = ", ".join(all_voices[vi])
            lines.append(f"  # ${vi:02X} — {alg_str} — used by {used_by}")

            # Find range stats across all FM channels for this voice
            # Skip 0-note entries (sentinel values 999/-1)
            note_bearing = [
                ch_an.voice_stats[vi]
                for ch_an in fm_channels
                if vi in ch_an.voice_stats and ch_an.voice_stats[vi].note_count > 0
            ]
            if not note_bearing:
                min_sem = max_sem = None
            else:
                min_sem = min(vs.min_semitone for vs in note_bearing)
                max_sem = max(vs.max_semitone for vs in note_bearing)
            has_trans = any(
                ch_an.has_transpose_change
                for ch_an in fm_channels
                if vi in ch_an.voice_stats
            )
            root_comment = "# root: safe (no smpsChangeTransposition)" if not has_trans else "# root: unsafe — channel uses smpsChangeTransposition"
            lines.append(f"  {vi}:")
            if min_sem is None:
                lines.append(f"    # (no notes played — voice switched to but never triggered)")
            else:
                lines.append(f"    - low:  {semitone_to_note_name(min_sem)}")
                lines.append(f"      high: {semitone_to_note_name(max_sem)}")
                lines.append(f"      mod_instrument: ???")
                lines.append(f"      {root_comment}")
                lines.append(f"      # root: ???")

    yaml_text = "\n".join(lines)
    syntax = Syntax(yaml_text, "yaml", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, title="Suggested YAML Skeleton", border_style="bright_blue"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyse a Sonic 1 SMPS assembly file and display structured info"
    )
    parser.add_argument('song', help="Path to the .asm file")
    parser.add_argument('--config', '-c', help="Optional YAML config to diff against")
    parser.add_argument('--region', choices=['ntsc', 'pal'], default='ntsc',
                        help="Console region for BPM derivation (default: ntsc)")
    args = parser.parse_args()

    if not os.path.exists(args.song):
        console.print(f"[red]Error:[/red] File not found: {args.song}")
        sys.exit(1)

    # Parse
    smps_parser = SmpsParser()
    song = smps_parser.parse_file(args.song)

    # Load config if provided
    config = None
    if args.config:
        if not os.path.exists(args.config):
            console.print(f"[red]Error:[/red] Config file not found: {args.config}")
            sys.exit(1)
        config = ConversionConfig.from_yaml(args.config)

    # Analyse
    analysis = analyze_song(song, args.song, config)

    # Render
    render_header(analysis, args.region)

    for ch_an in analysis.channels:
        if ch_an.channel_type == "DAC":
            render_channel_dac(ch_an, config)
        elif ch_an.channel_type == "FM":
            render_channel_fm(ch_an, config)
        else:
            render_channel_psg(ch_an, config)

    render_voices_table(analysis)

    if config is not None:
        render_config_coverage(analysis)

    render_yaml_skeleton(analysis, args.region)


if __name__ == '__main__':
    main()
