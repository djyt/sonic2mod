#!/usr/bin/env python3
"""SMPS song analyser — Rich-formatted analysis of a parsed SMPS file.

Usage:
    python analyze.py song.asm [--config configs/song.yaml] [--region ntsc|pal]

Requires: pip install rich
"""

import argparse
import os
import sys
from dataclasses import dataclass

try:
    from rich import box
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
except ImportError:
    print("Error: 'rich' is required. Install with: pip install rich")
    sys.exit(1)

from core.analysis import (
    DAC_NATIVE_INFO,
    DAC_SAMPLE_GROUPS,
    PARTIAL_EFFECTS,
    UNSUPPORTED_EFFECTS,
    ChannelAnalysis,
    PsgToneStats,
    SongAnalysis,
    analyze_song,
    semitone_to_note_name,
    suggest_transpose,
)
from core.config import (
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    find_settings,
    load_settings,
    rate3_synth_root_issues,
)
from core.mod import PERIOD_TABLE, ModFile, ModNote, db_to_mod_volume
from core.smps import (
    PSG_STEP_DB,
    TL_STEP_DB,
    carrier_names,
    flag_name,
    fm_level_db,
    psg_tone2_divider,
    synth_note_name,
)
from core.source import read_song
from core.ui import branding, cli_console

console = cli_console(highlight=True)


from core.audio import db_to_gain
from core.version import get_version as _get_version

_ALG_TOPOLOGY = {
    0: "1→2→3→4",
    1: "(1,2)→3→4",
    2: "(1+(2→3))→4",
    3: "((1→2)+3)→4",
    4: "(1→2)+(3→4)",
    5: "1→(2+3+4)",
    6: "(1,2)→(3+4)",
    7: "1+2+3+4",
}


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


# YAML-compatible note name table (uses 's' suffix for sharps, e.g. Fs not F#)
_sem_to_yaml = synth_note_name   # YAML config note name, e.g. 'C3', 'Fs2', 'As4'


def _note_in_octave2(semitone: int) -> str:
    """Place the note letter of semitone in octave 2, as a valid ModNote name.

    e.g. C6 → 'C2', C#5 → 'Cs2', F#5 → 'Fs2'
    """
    return ModNote(12 + semitone % 12).name


def _noise_root_for_synth(note_letter: int, synth_freq: float, amiga_clock: int) -> str:
    """Return the lowest MOD octave (≥ 2) where target_rate > 2*synth_freq, as a valid ModNote name.

    Ensures the synthesized LFSR frequency stays below Nyquist so there is no
    aliasing.  For low-frequency noise (e.g. Marble Zone C7 ≈ 2093 Hz) octave 2
    satisfies the constraint; for high-frequency noise (e.g. GHZ C9 ≈ 8383 Hz)
    the function steps up to octave 3.
    """
    for octave in range(2, 4):
        root_idx = (octave - 1) * 12 + note_letter
        period = PERIOD_TABLE[root_idx] if root_idx < len(PERIOD_TABLE) else 0
        if period == 0:
            break
        if amiga_clock / period > 2.0 * synth_freq:
            return ModNote(root_idx).name
    return ModNote(24 + note_letter).name


# sample_list volume of a single-carrier FM voice at TL offset 0, centred.  Fitted to the volumes
# measured against the VGZs in docs/audits/ (13 instruments, Title Screen + GHZ): each implies a
# value between 68 and 92, median 76 — so expect the skeleton's numbers to be within ~2 dB.
_FM_K = 76.0
_PSG_TONE_VOLUME = 16     # sample_list volume of a PSG tone at attenuation 0 (GHZ measures within 1 dB)
_PSG_NOISE_VOLUME = 16    # ... of PSG noise at attenuation 0


def _settings() -> tuple[SynthesisSettings, PsgSynthesisSettings]:
    """configs/settings.yaml, so the skeleton models levels and rates the way the converter will."""
    try:
        return load_settings(find_settings())
    except ValueError:
        return SynthesisSettings(), PsgSynthesisSettings()


def _db_volume(base: int, db: float) -> int:
    """A sample_list volume scaled by a dB offset; never 0, which would be a useless suggestion."""
    return db_to_mod_volume(base, db, minimum=1)


def _channel_has_notes(ch_an: ChannelAnalysis) -> bool:
    if ch_an.channel_type == "DAC":
        return bool(ch_an.dac_counts)
    if ch_an.channel_type == "FM":
        return any(vs.note_count > 0 for vs in ch_an.voice_stats.values())
    # PSG
    return any(ts.note_count > 0 for ts in ch_an.psg_tone_stats.values())


def _suggest_target_speed(tempo_divider: int, tempo_modifier: int, ticks_per_row: int, fps: int = 60) -> int:
    """The speed (2–8) whose whole-number BPM is closest to the driver's tempo, BPM within 32–255.

    A MOD BPM is an integer; speed changes how many MOD ticks a row has, not the row grid, so it
    is free to choose (core.config.bpm_rounding_options).  Ties go to the smaller speed.  Speed 1
    is the fallback when nothing fits.
    """
    from core.config import bpm_rounding_options
    options = bpm_rounding_options(tempo_divider, tempo_modifier, ticks_per_row, fps)
    return options[0]["speed"] if options else 1


_TOP_NOTE = ModNote.B3.value   # the highest MOD note
_TRANSPOSE_COMMENT = "  # smpsChangeTransposition active — verify root is correct"
_FORM_PREFIX = "form $"        # psg_tone_stats key of an smpsPSGform, e.g. "form $E7"


def _split_point(low: int, high: int) -> int | None:
    """Last semitone of the lower sample where low..high runs past B3 from its octave-2 root; None if one fits."""
    top = low + (_TOP_NOTE - (low % 12 + 12))
    return top if high > top else None


def _upper_root(low: int, high: int) -> str:
    """Root of a split range's upper entry: octave 2, or octave 1 where the range would run past B3."""
    note_class = low % 12
    in_octave2 = 12 + note_class
    return ModNote(note_class if in_octave2 + (high - low) > _TOP_NOTE else in_octave2).name


def _range_entry(low: int, high: int, inst: int, root: str) -> list[str]:
    """One `- low: … root:` entry of a voice_map / psg_voice_map range list."""
    return [
        f"    - low:  {_sem_to_yaml(low)}",
        f"      high: {_sem_to_yaml(high)}",
        f"      mod_instrument: {inst}",
        f"      root: {root}",
    ]


@dataclass
class _FmVoice:
    """An FM voice's instrument(s): `inst`, and `inst + 1` above `split` when its range needs two."""
    index: int
    inst: int
    alg: str                 # "alg 4: (1→2)+(3→4), fb 3"
    used_by: str             # "FM1, FM5"
    min_sem: int | None      # None: switched to, never played
    max_sem: int | None
    has_trans: bool          # a channel using it runs smpsChangeTransposition
    split: int | None
    volume: int = 32
    volume_note: str = ""    # "TL FM1 $0A → 41, …"


@dataclass
class _PsgNoise:
    form_byte: int
    label: str               # "form $E7"
    inst: int
    stats: PsgToneStats
    channel: ChannelAnalysis


@dataclass
class _PsgTone:
    label: str
    inst: int
    stats: PsgToneStats
    split: int | None        # as _FmVoice.split


class _Skeleton:
    """The suggested config for a song.  Instruments are numbered DAC → FM voices → PSG."""

    def __init__(self, analysis: SongAnalysis, region: str):
        self._analysis = analysis
        self._region = region
        self._active = [ch for ch in analysis.channels if _channel_has_notes(ch)]
        self._fm = [ch for ch in self._active if ch.channel_type == "FM"]
        self._psg = [ch for ch in self._active if ch.channel_type == "PSG"]
        self._synth, self._psg_synth = _settings()
        self._pan_law_db = self._synth.fm_pan_law_db   # a hard-panned note vs a centred one
        self._next_inst = 1

        self._dac_items: list[tuple[str, int, int]] = []   # (name, inst, count)
        self._dac_bases: list[tuple[str, int]] = []        # (base_name, inst) — one per instrument slot
        self._fm_voices: list[_FmVoice] = []
        self._noise: list[_PsgNoise] = []
        self._tones: list[_PsgTone] = []
        self._assign_dac()
        self._assign_fm()
        self._assign_psg()

    def text(self) -> str:
        lines = self._header()
        lines += self._sample_list()
        lines += self._dac_samples()
        lines += self._voice_map()
        lines += self._psg_map()
        lines += self._psg_voice_map()
        return "\n".join(lines)

    def _take(self, count: int) -> int:
        """The next free instrument slot, `count` slots reserved from it."""
        inst = self._next_inst
        self._next_inst += count
        return inst

    # --- instrument assignment ---

    def _assign_dac(self):
        """One slot per DAC sample, most played first; timpani variants share their base's slot."""
        seen: set[str] = set()
        inst_by_name: dict[str, int] = {}
        for ch_an in self._active:
            if ch_an.channel_type != "DAC":
                continue
            for name, count in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
                if name in seen:
                    continue
                seen.add(name)

                base = DAC_SAMPLE_GROUPS.get(name, name)
                if base not in inst_by_name:
                    inst_by_name[base] = self._take(1)
                    self._dac_bases.append((base, inst_by_name[base]))
                inst_by_name[name] = inst_by_name[base]
                self._dac_items.append((name, inst_by_name[name], count))

    def _assign_fm(self):
        used_by: dict[int, list[str]] = {}
        for ch_an in self._fm:
            for vi in ch_an.voice_stats:
                used_by.setdefault(vi, []).append(ch_an.name)

        for vi in sorted(used_by):
            playing = [ch_an for ch_an in self._fm
                       if vi in ch_an.voice_stats and ch_an.voice_stats[vi].note_count > 0]
            stats = [ch_an.voice_stats[vi] for ch_an in playing]
            min_sem = min(vs.min_semitone for vs in stats) if stats else None
            max_sem = max(vs.max_semitone for vs in stats) if stats else None
            split = _split_point(min_sem, max_sem) if min_sem is not None and max_sem is not None else None
            voice = _FmVoice(
                index=vi,
                inst=self._take(2 if split is not None else 1),
                alg=self._algorithm(vi),
                used_by=", ".join(used_by[vi]),
                min_sem=min_sem,
                max_sem=max_sem,
                has_trans=any(ch_an.has_transpose_change for ch_an in self._fm if vi in ch_an.voice_stats),
                split=split,
            )
            if playing:
                self._set_fm_volume(voice, playing)
            self._fm_voices.append(voice)

    def _algorithm(self, vi: int) -> str:
        voice_def = next((v for v in self._analysis.song.voices if v.index == vi), None)
        if not voice_def:
            return "unknown"
        topo = _ALG_TOPOLOGY.get(voice_def.algorithm, "?")
        return f"alg {voice_def.algorithm}: {topo}, fb {voice_def.feedback}"

    def _set_fm_volume(self, voice: _FmVoice, playing: list[ChannelAnalysis]):
        """The voice's level on each channel it plays on — TL offset and pan.  The dominant channel's
        level becomes the sample_list volume (what fm_volume_scaling: baked expects; it puts Cxx on
        the rest)."""
        vi = voice.index
        levels = {ch_an.name: (ch_an.voice_stats[vi].modal_volume, ch_an.voice_stats[vi].modal_hard_pan)
                  for ch_an in playing}
        dominant = max(playing, key=lambda c: c.voice_stats[vi].note_count)
        voice.volume = self._fm_volume(levels[dominant.name])

        per_ch = [f"{name} ${lv[0] & 0xFF:02X}{' panned' if lv[1] else ''} → {self._fm_volume(lv)}"
                  for name, lv in levels.items()]
        voice.volume_note = "TL " + ", ".join(per_ch)
        if len({self._fm_volume(lv) for lv in levels.values()}) > 1:
            voice.volume_note += (f"  (volume is {dominant.name}'s; fm_volume_scaling: baked "
                                  "puts Cxx on the others)")

    def _fm_volume(self, lv: tuple[int, bool]) -> int:
        return max(1, min(64, round(_FM_K * db_to_gain(fm_level_db(lv[0], lv[1], self._pan_law_db)))))

    def _assign_psg(self):
        """One slot per smpsPSGform byte and per tone label (two for a split range).  A label every
        channel plays only as a noise envelope gets none."""
        seen: set[str] = set()
        for ch_an in self._psg:
            for label, ts in ch_an.psg_tone_stats.items():
                if label in seen:
                    continue
                seen.add(label)

                if label.startswith(_FORM_PREFIX):
                    form_byte = int(label[len(_FORM_PREFIX):], 16)
                    self._noise.append(_PsgNoise(form_byte, label, self._take(1), ts, ch_an))
                    continue
                if self._is_noise_envelope(label):
                    continue

                split = _split_point(ts.min_semitone, ts.max_semitone) if ts.note_count else None
                self._tones.append(_PsgTone(label, self._take(2 if split is not None else 1), ts, split))

    def _is_noise_envelope(self, label: str) -> bool:
        """Every channel playing the label is a noise channel (one with an smpsPSGform)."""
        users = [ch for ch in self._psg if label in ch.psg_tone_stats]
        return bool(users) and all(_has_noise(ch) for ch in users)

    def _psg_attenuation(self, label: str) -> int:
        """The label's attenuation on the channel that plays it most (psg_volume_scaling: baked)."""
        users = [ch.psg_tone_stats[label] for ch in self._psg
                 if label in ch.psg_tone_stats and ch.psg_tone_stats[label].note_count > 0]
        return max(users, key=lambda t: t.note_count).modal_volume if users else 0

    # --- sections ---

    def _header(self) -> list[str]:
        song = self._analysis.song
        fname = os.path.basename(self._analysis.file_path)
        stem = os.path.splitext(fname)[0]
        speed = _suggest_target_speed(song.header.tempo_divider, song.header.tempo_modifier, ticks_per_row=1)
        lines = [
            "# Generated by analyze.py — fill in root values (synth_root is derived from the song)",
            f"# Song: {fname}",
            "",
            f"name: {stem}",
            f"input_file: {self._analysis.file_path}",
            f"output_file: output/{stem.replace(' ', '_')}.mod",
            "samples_dir: \"samples/\"",
            "",
            "auto_bpm: true",
            f"target_speed: {speed}",
            "ticks_per_row: 1",
            f"# {ModFile.round_up_channels(len(self._active))} MOD channels, derived from the channels below;"
            " set num_mod_channels: only to pad for a spare Fxx/Dxx channel",
            f"region: {self._region}",
            "",
            "channels:",
        ]
        for mod_ch, ch_an in enumerate(self._active):
            lines.append(f"  - source: {'DAC' if ch_an.channel_type == 'DAC' else ch_an.name}")
            lines.append(f"    mod_channel: {mod_ch}")
        return lines

    def _sample_list(self) -> list[str]:
        lines = ["", "sample_list:"]
        if self._dac_items:
            lines.append("  # --- percussion ---")
            for base_name, inst in self._dac_bases:
                lines.append(f"  - [{inst}, \"{base_name[1:].lower()}.raw\", 64, 0]")

        if self._fm_voices:
            lines.append(f"  # FM volumes bake in each channel's level: TL offset (smpsHeaderFM volume + smpsAlterVol, "
                         f"{TL_STEP_DB} dB/step)")
            lines.append(f"  # and −{self._pan_law_db:g} dB when hard-panned:  {_FM_K:g} × 10^(dB/20), max 64.  "
                         "Starting points (~2 dB) —")
            lines.append("  # measure with tools/vgm_compare.py; every channel sharing an instrument should read the same error.")
        for v in self._fm_voices:
            lines.append(f"  # --- voice ${v.index:02X}: {v.alg} — {v.used_by} ---")
            if v.volume_note:
                lines.append(f"  #     {v.volume_note}")
            if v.split is None:
                lines.append(f"  - [{v.inst}, \"fm_v{v.index:02x}.raw\", {v.volume}, 0]")
                continue
            lines.append(f"  - [{v.inst}, \"fm_v{v.index:02x}_lo.raw\", {v.volume}, 0]")
            lines.append(f"  - [{v.inst + 1}, \"fm_v{v.index:02x}_hi.raw\", {v.volume}, 0]")

        if self._noise or self._tones:
            lines.append(f"  # PSG volumes bake in the track attenuation ({PSG_STEP_DB:g} dB/step): "
                         f"tone {_PSG_TONE_VOLUME} / noise {_PSG_NOISE_VOLUME} at attenuation 0.")
        for n in self._noise:
            lines.append(f"  # --- PSG noise ({n.label}) ---")
            lines.append(self._psg_sample(n.inst, "psg_noise.raw", _PSG_NOISE_VOLUME, n.label))
        for t in self._tones:
            lines.append(f"  # --- PSG tone {t.label} ---")
            if t.split is None:
                lines.append(self._psg_sample(t.inst, f"psg_{t.label}.raw", _PSG_TONE_VOLUME, t.label))
                continue
            lines.append(self._psg_sample(t.inst, f"psg_{t.label}_lo.raw", _PSG_TONE_VOLUME, t.label))
            lines.append(self._psg_sample(t.inst + 1, f"psg_{t.label}_hi.raw", _PSG_TONE_VOLUME, t.label))
        return lines

    def _psg_sample(self, inst: int, fname: str, base: int, label: str) -> str:
        att = self._psg_attenuation(label)
        return (f"  - [{inst}, \"{fname}\", {_db_volume(base, -PSG_STEP_DB * att)}, 0]"
                + (f"   # attenuation {att} (−{att * PSG_STEP_DB:g} dB)" if att else ""))

    def _dac_samples(self) -> list[str]:
        if not self._dac_items:
            return []

        lines = ["", "dac_samples:"]
        for name, inst, count in self._dac_items:
            native_note, native_rate = DAC_NATIVE_INFO.get(name, ("C3", 8000))
            lines.append(f"  # {name}: ×{count} — native ~{native_rate} Hz → note {native_note}")
            lines.append(f"  - name: {name}")
            lines.append(f"    mod_instrument: {inst}")
            lines.append(f"    mod_note: {native_note}")
        return lines

    def _voice_map(self) -> list[str]:
        if not self._fm_voices:
            return []

        lines = ["", "voice_map:"]
        for v in self._fm_voices:
            lines.append(f"  # ${v.index:02X} — {v.alg} — used by {v.used_by}")
            lines.append(f"  {v.index}:")
            lines += self._fm_ranges(v)
        return lines

    def _fm_ranges(self, v: _FmVoice) -> list[str]:
        """No synth_root: the converter renders at the pitch the chip plays for `low`."""
        if v.min_sem is None or v.max_sem is None:
            return ["    # (no notes played — voice switched to but never triggered)"]

        comment = _TRANSPOSE_COMMENT if v.has_trans else ""
        if v.split is None:
            return _range_entry(v.min_sem, v.max_sem, v.inst, _note_in_octave2(v.min_sem) + comment)
        return (_range_entry(v.min_sem, v.split, v.inst, _note_in_octave2(v.min_sem) + comment)
                + _range_entry(v.split + 1, v.max_sem, v.inst + 1, _upper_root(v.split + 1, v.max_sem) + comment))

    def _psg_map(self) -> list[str]:
        if not self._noise:
            return []

        lines = ["", "psg_map:"]
        for n in self._noise:
            lines += self._noise_entry(n)
        return lines

    def _noise_entry(self, n: _PsgNoise) -> list[str]:
        ts = n.stats
        noise_rate = n.form_byte & 0x03
        min_sem = ts.min_semitone if ts.note_count > 0 else 45
        transpose = n.channel.initial_transpose

        tone2_n: int | None = None
        shift_hz = 0.0
        root_name = _note_in_octave2(min_sem)
        if noise_rate == 3 and ts.note_count > 0:
            # The LFSR is clocked by tone channel 2, whose divider the driver looks up in
            # PSGFrequencies from the channel's own note — not a chromatic extrapolation:
            # nMaxPSG (index 69) is divider 0 → N=1, not the ~7 kHz an "A8" would give.
            tone2_n = psg_tone2_divider(0x81 + min_sem, transpose)
            shift_hz = self._psg_synth.clock_rate / (32.0 * tone2_n)
            # root must satisfy Nyquist for the LFSR shift rate where that is achievable;
            # above it the highest-rate root is the best a MOD sample can do.
            root_name = _noise_root_for_synth(min_sem % 12, shift_hz, self._synth.amiga_clock)

        noise_kind = "white" if n.form_byte & 0x04 else "periodic"
        lines = [
            f"  0x{n.form_byte:02X}:                    # {n.label}: {noise_kind} noise, rate {noise_rate} "
            "(both read from the byte)",
            f"    mod_instrument: {n.inst}",
            f"    root: {root_name}",
        ]
        if tone2_n is not None:
            # Not emitted as a key: the converter derives the divider from the song itself
            # (derive_rate3_dividers); an explicit tone2_n here would only shadow that.
            note_desc = semitone_to_note_name(min_sem)
            lines.append(f"    # LFSR divider is derived from the song: {tone2_n} for n{note_desc} "
                         f"{transpose:+d} → {shift_hz:,.0f} Hz")
            if ts.max_semitone != ts.min_semitone:
                # Pitched noise: anchor the sample at the lowest note so MOD playback speed
                # follows the melody (one static LFSR rate per sample is the approximation).
                lines.append(f"    low: {_sem_to_yaml(min_sem)}                # n{note_desc} plays at root; "
                             f"notes up to n{semitone_to_note_name(ts.max_semitone)} shift the playback rate")

        # The envelope is not a key: the converter derives it from the song (the header
        # voice or the last smpsPSGvoice); `envelopes: {label: inst}` gives a label its own sample.
        first = _first_envelope(n.channel)
        lines.append(f"    # envelope derived from the song: {first or 'header voice'}")
        # A label the noise also plays under keeps the derived envelope's sample (the converter
        # warns noise_envelopes); Scrap Brain's fTone_08 hats got their own this way
        for label, label_stats in n.channel.psg_tone_stats.items():
            if label.startswith(_FORM_PREFIX) or label == first or not label_stats.note_count:
                continue
            notes = f"{label_stats.note_count} note" + ("s" if label_stats.note_count != 1 else "")
            lines.append(f"    # also plays under {label} ({notes}): give it its own "
                         f"sample with envelopes: {{{label}: <instrument>}}")
        return lines

    def _psg_voice_map(self) -> list[str]:
        if not self._tones:
            return []

        lines = ["", "psg_voice_map:"]
        for t in self._tones:
            lines.append(f"  {t.label}:")
            lines += self._tone_ranges(t)
        return lines

    def _tone_ranges(self, t: _PsgTone) -> list[str]:
        ts = t.stats
        if ts.note_count == 0:
            return ["    # (no notes played — label switched to but never triggered)"]

        if t.split is not None:
            return (_range_entry(ts.min_semitone, t.split, t.inst, _note_in_octave2(ts.min_semitone))
                    + _range_entry(t.split + 1, ts.max_semitone, t.inst + 1,
                                   _upper_root(t.split + 1, ts.max_semitone)))
        return [
            f"    low:  {_sem_to_yaml(ts.min_semitone)}",
            f"    high: {_sem_to_yaml(ts.max_semitone)}",
            f"    mod_instrument: {t.inst}",
            f"    root: {_note_in_octave2(ts.min_semitone)}",
            f"    synth_root: {_sem_to_yaml(ts.min_semitone)}",
        ]


def _has_noise(ch_an: ChannelAnalysis) -> bool:
    """The channel runs an smpsPSGform: a noise channel."""
    return any(label.startswith(_FORM_PREFIX) for label in ch_an.psg_tone_stats)


def _first_envelope(ch_an: ChannelAnalysis) -> str:
    """A noise channel's first voice label — the envelope its notes start under; "" if none."""
    if not _has_noise(ch_an):
        return ""
    return next((label for label in ch_an.psg_tone_stats if not label.startswith(_FORM_PREFIX)), "")


def render_yaml_skeleton(analysis: SongAnalysis, region: str, write_path: str | None = None):
    """Print (or write) a suggested YAML skeleton."""
    yaml_text = _Skeleton(analysis, region).text()

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
    branding(console, "SONIC2MOD", _get_version())

    parser = argparse.ArgumentParser(
        description="Analyse a Sonic 1 SMPS assembly file and display structured info"
    )
    parser.add_argument('song', help="Path to the .asm file, or a .vgm / .vgz rip")
    parser.add_argument('--config', '-c', help="Optional YAML config to diff against")
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

    # Parse (an asm), or lift (a VGM rip)
    try:
        song = read_song(args.song)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

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
