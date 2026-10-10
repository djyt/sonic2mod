#!/usr/bin/env python3
"""SMPS song analyser — Rich-formatted analysis of a parsed SMPS file.

Usage:
    python analyze.py song.asm [--config configs/song.yaml] [--region ntsc|pal]

Requires: pip install rich
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

from core.chips import carrier_names

try:
    from rich import box
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
except ImportError:
    print("Error: 'rich' is required. Install with: pip install rich")
    sys.exit(1)

from core.analysis import (
    PARTIAL_EFFECTS,
    UNSUPPORTED_EFFECTS,
    ChannelAnalysis,
    SongAnalysis,
    analyze_song,
    semitone_to_note_name,
    suggest_transpose,
    yaml_skeleton,
)
from core.config import (
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    find_settings,
    load_settings,
    parse_number,
    rate3_synth_root_issues,
)
from core.plan import derive_config, load_config
from core.smps import (
    ChannelType,
    flag_name,
)
from core.source import read_dac, read_song
from core.ui import add_variant_argument, branding, cli_console

console = cli_console(highlight=True)


from core.version import get_version as _get_version

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _note_range_str(ch: ChannelAnalysis) -> str:
    if ch.min_semitone is None:
        return "—"
    assert ch.max_semitone is not None
    lo = semitone_to_note_name(ch.min_semitone)
    hi = semitone_to_note_name(ch.max_semitone)
    return f"{lo}–{hi} (semitones {ch.min_semitone}–{ch.max_semitone})"


def _by_name(effect_counts: dict) -> list:
    """(flag, count) in the order of the flags' SMPS2ASM names."""
    return sorted(effect_counts.items(), key=lambda kv: flag_name(kv[0]))


def _unsupported_summary(effect_counts: dict) -> str:
    parts = []
    for et, n in _by_name(effect_counts):
        if et in UNSUPPORTED_EFFECTS:
            parts.append(f"{flag_name(et)} ×{n}")
    return "  ".join(parts)


def _partial_summary(effect_counts: dict) -> list[str]:
    parts = []
    for et, n in _by_name(effect_counts):
        if et in PARTIAL_EFFECTS:
            parts.append(f"{flag_name(et)} ×{n}  ({PARTIAL_EFFECTS[et]})")
    return parts


def _normal_effect_summary(effect_counts: dict) -> str:
    """Effects excluding unsupported/partial ones."""
    parts = []
    for et, n in _by_name(effect_counts):
        if et not in UNSUPPORTED_EFFECTS and et not in PARTIAL_EFFECTS:
            parts.append(f"{flag_name(et)} ×{n}")
    return "  ".join(parts) if parts else "—"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def render_header(analysis: SongAnalysis, region: str):
    song = analysis.song
    fname = os.path.basename(analysis.file_path)
    h = song.header

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
        title="[cyan bold]DAC[/cyan bold]",
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
    if ch.initial_transpose != 0 and ch.min_semitone is not None and ch.max_semitone is not None:
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
            "[yellow]smpsChangeTransposition: YES[/yellow]  "
            "[dim]← do NOT use root on this channel[/dim]"
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

    lines.extend(f"[yellow]Partial:[/yellow] {part}" for part in _partial_summary(ch.effect_counts))

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

    if ch.min_semitone is not None and ch.max_semitone is not None:
        lines.append(f"Note range: {_note_range_str(ch)}")
        sug = suggest_transpose(ch.min_semitone, ch.max_semitone)
        lines.append(f"  → suggested transpose: [bold]{sug:+d}[/bold]")

    if ch.psg_tone_stats:
        lines.append("PSG tones used:")
        for label, ts in ch.psg_tone_stats.items():
            if ts.note_count == 0:
                range_str = "—"
            else:
                lo = semitone_to_note_name(ts.min_semitone)
                hi = semitone_to_note_name(ts.max_semitone)
                range_str = f"{lo}–{hi}"
            lines.append(
                f"  [yellow]{label}[/yellow]  {range_str}  "
                f"({ts.note_count} notes, {ts.switch_count} switches)"
            )

    normal_eff = _normal_effect_summary(ch.effect_counts)
    if normal_eff != "—":
        lines.append(f"Effects: {normal_eff}")

    unsup = _unsupported_summary(ch.effect_counts)
    if unsup:
        lines.append(f"[dim]Unsupported:[/dim] {unsup}")

    lines.extend(f"[yellow]Partial:[/yellow] {part}" for part in _partial_summary(ch.effect_counts))

    if config is not None and ch.config_enabled is False:
        lines.append("[dim]config: disabled[/dim]")
    elif config is not None and ch.psg_tone_stats:
        cov_parts = []
        for label in ch.psg_tone_stats:
            if label.startswith("form $"):
                form_byte = int(label[6:], 16)
                entry = config.psg_map.get(form_byte)
            else:
                _pvm_entries = config.psg_voice_map.get(label)
                entry = _pvm_entries[0] if _pvm_entries else None
            if entry is not None:
                cov_parts.append(f"[green]✓[/green] {label} → inst {entry.mod_instrument}")
            else:
                cov_parts.append(f"[red]✗[/red] {label} not configured")
        if cov_parts:
            lines.append("Config:  " + "  ".join(cov_parts))

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
        if ch_an.channel_type == ChannelType.FM:
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
        carriers = "+".join(carrier_names(alg))
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

        if ch_an.channel_type == ChannelType.DAC:
            dac_cfgs = {d.name: d for d in config.dac_samples}
            for name, _ in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
                if name in dac_cfgs:
                    d = dac_cfgs[name]
                    cov = f"[green]✓[/green] inst {d.mod_instrument}, note {d.mod_note}"
                else:
                    cov = "[red]✗[/red] not configured"
                table.add_row(ch_an.name, name, "—", cov)

        elif ch_an.channel_type == ChannelType.FM:
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
            if not ch_an.psg_tone_stats:
                enabled_str = "" if ch_an.config_enabled else "[dim]disabled[/dim]"
                table.add_row(ch_an.name, "—", "—", enabled_str or "[dim]PSG (no tones)[/dim]")
            else:
                for label, ts in ch_an.psg_tone_stats.items():
                    if ts.note_count == 0:
                        rng = "—"
                    else:
                        lo = semitone_to_note_name(ts.min_semitone)
                        hi = semitone_to_note_name(ts.max_semitone)
                        rng = f"{lo}–{hi}"
                    if label.startswith("form $"):
                        form_byte = int(label[6:], 16)
                        entry = config.psg_map.get(form_byte)
                    else:
                        _pvm_entries = config.psg_voice_map.get(label)
                        entry = _pvm_entries[0] if _pvm_entries else None
                    if entry is not None:
                        cov = f"[green]✓[/green] inst {entry.mod_instrument}"
                    else:
                        cov = "[red]✗[/red] not configured"
                    table.add_row(ch_an.name, label, rng, cov)

    console.print(table)

    for issue in rate3_synth_root_issues(analysis.config):
        side = "above" if issue['above'] else "below"
        console.print(
            f"[yellow]![/yellow] [bold]{issue['context']}[/bold]: rate-3 noise synth_root "
            f"{issue['synth_root']} is {side} the driver's PSG table (C3–Gs8) — delete it; the "
            "converter derives the divider from the song"
        )


def _settings() -> tuple[SynthesisSettings, PsgSynthesisSettings]:
    """configs/settings.yaml, so the skeleton models levels and rates the way the converter will."""
    try:
        return load_settings(find_settings())
    except ValueError:
        return SynthesisSettings(), PsgSynthesisSettings()


def render_yaml_skeleton(analysis: SongAnalysis, region: str, write_path: str | None = None):
    """Print (or write) a suggested YAML skeleton."""
    yaml_text = yaml_skeleton(analysis, region, *_settings())

    if write_path:
        with open(write_path, 'w', encoding='utf-8') as f:
            f.write(yaml_text + "\n")
        console.print(f"[green]Wrote YAML skeleton to:[/green] {write_path}")
    else:
        syntax = Syntax(yaml_text, "yaml", theme="monokai", line_numbers=False)
        console.print(Panel(syntax, title="Suggested YAML Skeleton", border_style="bright_blue"))


def render_derived_config(song, args, write_path: str | None = None):
    """A ROM song's starter config: the minimal one, completed as convert.py would (core.plan.derive)."""
    stated = {"input_file": args.song, "rom_song": args.rom_song}
    data = derive_config(stated, song, Path(f"configs/{Path(args.song).stem}.yaml"), _settings()[0],
                         read_dac(args.song)).data
    yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None)
    if write_path:
        Path(write_path).write_text(yaml_text, encoding="utf-8")
        console.print(f"[green]Wrote the derived config to:[/green] {write_path}  "
                      "[dim](a minimal config needs only name, input_file and rom_song)[/dim]")
        return
    console.print(Panel(Syntax(yaml_text, "yaml", theme="monokai", line_numbers=False),
                        title="Derived config (core.plan.derive)", border_style="bright_blue"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    branding(console, "SONIC2MOD", _get_version())

    parser = argparse.ArgumentParser(
        description="Analyse a Sonic 1 SMPS assembly file and display structured info"
    )
    parser.add_argument('song', help="Path to the .asm file, a ROM (with --rom-song), or a .vgm / .vgz rip")
    parser.add_argument('--rom-song', metavar='ID', help="With a ROM: the sound to analyse ($81, 0x81)")
    parser.add_argument('--config', '-c', help="Optional YAML config to diff against")
    add_variant_argument(parser)
    parser.add_argument('--version', action='version',
                        version=f"sonic2mod {_get_version()}")
    parser.add_argument('--region', choices=['ntsc', 'pal'], default='ntsc',
                        help="Console region for BPM derivation (default: ntsc)")
    parser.add_argument('--write', '-w', metavar='FILE',
                        help="Write YAML skeleton to FILE instead of printing to terminal")
    args = parser.parse_args()

    if not os.path.exists(args.song):
        console.print(f"[red]Error:[/red] File not found: {args.song}")
        sys.exit(1)

    # Parse (an asm), read (a ROM) or lift (a VGM rip)
    try:
        song = read_song(args.song, rom_song=parse_number(args.rom_song, "--rom-song") if args.rom_song else None)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # Load config if provided
    config = None
    if args.config:
        if not os.path.exists(args.config):
            console.print(f"[red]Error:[/red] Config file not found: {args.config}")
            sys.exit(1)
        config = load_config(args.config, variant=args.variant)

    # Analyse
    analysis = analyze_song(song, args.song, config)

    # Render
    render_header(analysis, args.region)

    for ch_an in analysis.channels:
        if ch_an.channel_type == ChannelType.DAC:
            render_channel_dac(ch_an, config)
        elif ch_an.channel_type == ChannelType.FM:
            render_channel_fm(ch_an, config)
        else:
            render_channel_psg(ch_an, config)

    render_voices_table(analysis)

    if config is not None:
        render_config_coverage(analysis)

    if args.rom_song:
        render_derived_config(song, args, write_path=args.write)
    else:
        render_yaml_skeleton(analysis, args.region, write_path=args.write)


if __name__ == '__main__':
    main()
