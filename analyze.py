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
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
except ImportError:
    print("Error: 'rich' is required. Install with: pip install rich")
    sys.exit(1)

from core.analysis import (
    _CARRIER_LABELS_BY_ALG,
    DAC_NATIVE_INFO,
    DAC_SAMPLE_GROUPS,
    PARTIAL_EFFECTS,
    UNSUPPORTED_EFFECTS,
    ChannelAnalysis,
    SongAnalysis,
    analyze_song,
    semitone_to_note_name,
    suggest_transpose,
)
from core.config import ConversionConfig
from core.smps_parser import SmpsParser

console = Console(legacy_windows=False)


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


def _effect_summary(effect_counts: dict, label: str = "Effects") -> str:
    parts = [f"{et} ×{n}" for et, n in sorted(effect_counts.items())]
    return "  ".join(parts) if parts else "—"


def _unsupported_summary(effect_counts: dict) -> str:
    parts = []
    for et, n in sorted(effect_counts.items()):
        if et in UNSUPPORTED_EFFECTS:
            parts.append(f"{et} ×{n}")
    return "  ".join(parts)


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
                entry = config.psg_voice_map.get(label)
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
            for name, _ in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
                if name in dac_cfgs:
                    d = dac_cfgs[name]
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
                        entry = config.psg_voice_map.get(label)
                    if entry is not None:
                        cov = f"[green]✓[/green] inst {entry.mod_instrument}"
                    else:
                        cov = "[red]✗[/red] not configured"
                    table.add_row(ch_an.name, label, rng, cov)

    console.print(table)


# YAML-compatible note name table (uses 's' suffix for sharps, e.g. Fs not F#)
_YAML_CHROMATIC = ['C', 'Cs', 'D', 'Ds', 'E', 'F', 'Fs', 'G', 'Gs', 'A', 'As', 'B']


def _sem_to_yaml(semitone: int) -> str:
    """Convert semitone to YAML config note name (e.g. 'C3', 'Fs2', 'As4')."""
    return f"{_YAML_CHROMATIC[semitone % 12]}{semitone // 12}"


def _note_in_octave2(semitone: int) -> str:
    """Place the note letter of semitone in octave 2 (e.g. C6 → C2, Fs5 → Fs2)."""
    return f"{_YAML_CHROMATIC[semitone % 12]}2"


def render_yaml_skeleton(analysis: SongAnalysis, region: str, write_path: str | None = None):
    """Print (or write) a suggested YAML skeleton."""
    song = analysis.song
    fname = os.path.basename(analysis.file_path)

    lines: list[str] = [
        "# Generated by analyze.py — fill in root/synth_root values",
        f"# Song: {fname}",
        "",
        f"name: {os.path.splitext(fname)[0]}",
        f"input_file: {analysis.file_path}",
        f"output_file: output/{os.path.splitext(fname)[0].replace(' ', '_')}.mod",
        "samples_dir: \"samples/\"",
        "",
        "auto_bpm: true",
        f"target_speed: {song.header.tempo_modifier}",
        "ticks_per_row: 2",
        f"num_mod_channels: {len(analysis.channels)}",
        f"region: {region}",
        "",
        "channels:",
    ]

    for mod_ch, ch_an in enumerate(analysis.channels):
        ch_type = ch_an.channel_type
        lines.append(f"  - source: {'DAC' if ch_type == 'DAC' else ch_an.name}")
        lines.append(f"    mod_channel: {mod_ch}")

    # --- Assign instruments sequentially: DAC → FM voices → PSG ---
    inst_counter = 1

    # DAC
    dac_channels = [ch for ch in analysis.channels if ch.channel_type == "DAC"]
    dac_items: list[tuple[str, int, int]] = []   # (name, inst, count)
    dac_base_insts: list[tuple[str, int]] = []   # (base_name, inst) — one per instrument slot
    seen_dac: set[str] = set()
    dac_inst_by_name: dict[str, int] = {}
    for ch_an in dac_channels:
        for name, count in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
            if name not in seen_dac:
                seen_dac.add(name)
                base = DAC_SAMPLE_GROUPS.get(name, name)
                if base not in dac_inst_by_name:
                    dac_inst_by_name[base] = inst_counter
                    dac_base_insts.append((base, inst_counter))
                    inst_counter += 1
                dac_inst_by_name[name] = dac_inst_by_name[base]
                dac_items.append((name, dac_inst_by_name[name], count))

    # FM voices
    fm_channels = [ch for ch in analysis.channels if ch.channel_type == "FM"]
    all_voices: dict[int, list[str]] = {}
    for ch_an in fm_channels:
        for vi in ch_an.voice_stats:
            all_voices.setdefault(vi, []).append(ch_an.name)

    fm_items: list[tuple] = []  # (vi, inst, min_sem, max_sem, has_trans, alg_str, used_by)
    for vi in sorted(all_voices.keys()):
        voice_def = next((v for v in song.voices if v.index == vi), None)
        alg_str = f"Alg {voice_def.algorithm}, FB {voice_def.feedback}" if voice_def else "unknown"
        used_by = ", ".join(all_voices[vi])
        note_bearing = [
            ch_an.voice_stats[vi]
            for ch_an in fm_channels
            if vi in ch_an.voice_stats and ch_an.voice_stats[vi].note_count > 0
        ]
        min_sem = min(vs.min_semitone for vs in note_bearing) if note_bearing else None
        max_sem = max(vs.max_semitone for vs in note_bearing) if note_bearing else None
        has_trans = any(
            ch_an.has_transpose_change
            for ch_an in fm_channels
            if vi in ch_an.voice_stats
        )
        fm_items.append((vi, inst_counter, min_sem, max_sem, has_trans, alg_str, used_by))
        inst_counter += 1

    # PSG
    psg_channels = [ch for ch in analysis.channels if ch.channel_type == "PSG"]
    seen_psg: set[str] = set()
    psg_noise_items: list[tuple] = []   # (form_byte, label, inst, ts)
    psg_tone_items: list[tuple] = []    # (label, inst, ts)
    for ch_an in psg_channels:
        for label, ts in ch_an.psg_tone_stats.items():
            if label not in seen_psg:
                seen_psg.add(label)
                if label.startswith("form $"):
                    form_byte = int(label[6:], 16)
                    psg_noise_items.append((form_byte, label, inst_counter, ts))
                else:
                    psg_tone_items.append((label, inst_counter, ts))
                inst_counter += 1

    # --- sample_list ---
    lines.append("")
    lines.append("sample_list:")
    if dac_items:
        lines.append("  # --- percussion ---")
        for base_name, inst in dac_base_insts:
            lines.append(f"  - [{inst}, \"dac_{base_name}.raw\", 64, 0]")
    for vi, inst, _min_sem, _max_sem, _has_trans, alg_str, used_by in fm_items:
        lines.append(f"  # --- voice ${vi:02X}: {alg_str} — {used_by} ---")
        lines.append(f"  - [{inst}, \"fm_v{vi:02x}.raw\", 32, 0]")
    for _form_byte, label, inst, _ts in psg_noise_items:
        lines.append(f"  # --- PSG noise ({label}) ---")
        lines.append(f"  - [{inst}, \"psg_noise.raw\", 16, 0]")
    for label, inst, _ts in psg_tone_items:
        lines.append(f"  # --- PSG tone {label} ---")
        lines.append(f"  - [{inst}, \"psg_{label}.raw\", 32, 0]")

    # --- dac_samples ---
    if dac_items:
        lines.append("")
        lines.append("dac_samples:")
        for name, inst, count in dac_items:
            native_note, native_rate = DAC_NATIVE_INFO.get(name, ("C3", 8000))
            lines.append(f"  # {name}: ×{count} — native ~{native_rate} Hz → note {native_note}")
            lines.append(f"  - name: {name}")
            lines.append(f"    mod_instrument: {inst}")
            lines.append(f"    mod_note: {native_note}")

    # --- voice_map ---
    if fm_items:
        lines.append("")
        lines.append("voice_map:")
        for vi, inst, min_sem, max_sem, has_trans, alg_str, used_by in fm_items:
            lines.append(f"  # ${vi:02X} — {alg_str} — used by {used_by}")
            lines.append(f"  {vi}:")
            if min_sem is None or max_sem is None:
                lines.append("    # (no notes played — voice switched to but never triggered)")
            else:
                lines.append(f"    - low:  {_sem_to_yaml(min_sem)}")
                lines.append(f"      high: {_sem_to_yaml(max_sem)}")
                lines.append(f"      mod_instrument: {inst}")
                lines.append(f"      root: {_note_in_octave2(min_sem)}")
                lines.append(f"      synth_root: {_sem_to_yaml(min_sem)}")
                if has_trans:
                    lines.append(
                        "      # WARNING: channel uses smpsChangeTransposition"
                        " — remove root, use channel transpose instead"
                    )

    # --- psg_map ---
    if psg_noise_items:
        lines.append("")
        lines.append("psg_map:")
        for form_byte, label, inst, _ts in psg_noise_items:
            lines.append(f"  0x{form_byte:02X}:                    # {label}")
            lines.append(f"    mod_instrument: {inst}")
            lines.append("    root: A3")
            lines.append(f"    noise_rate: {form_byte & 0x03}")
            lines.append("    envelope: fTone_04")
            lines.append("    base_volume: 0")

    # --- psg_voice_map ---
    if psg_tone_items:
        lines.append("")
        lines.append("psg_voice_map:")
        for label, inst, ts in psg_tone_items:
            lines.append(f"  {label}:")
            if ts.note_count == 0:
                lines.append(f"    mod_instrument: {inst}")
                lines.append("    # (no notes — placeholder only)")
            else:
                lo = _sem_to_yaml(ts.min_semitone)
                hi = _sem_to_yaml(ts.max_semitone)
                lines.append(f"    low:  {lo}")
                lines.append(f"    high: {hi}")
                lines.append(f"    mod_instrument: {inst}")
                lines.append(f"    root: {_note_in_octave2(ts.min_semitone)}")
                lines.append(f"    synth_root: {_sem_to_yaml(ts.min_semitone)}")

    yaml_text = "\n".join(lines)

    if write_path:
        with open(write_path, 'w', encoding='utf-8') as f:
            f.write(yaml_text + "\n")
        console.print(f"[green]Wrote YAML skeleton to:[/green] {write_path}")
    else:
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
    parser.add_argument('--write', '-w', metavar='FILE',
                        help="Write YAML skeleton to FILE instead of printing to terminal")
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

    render_yaml_skeleton(analysis, args.region, write_path=args.write)


if __name__ == '__main__':
    main()
