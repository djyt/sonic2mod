#!/usr/bin/env python3
"""CLI entry point for Sonic 1 SFX rendering.

Runs a tick-accurate offline reimplementation of the Sonic 1 sound driver against
the YM2612 and SN76489 emulators, writing each effect as configs/sfx.yaml asks:
WAV or raw, 8 or 16 bits, its rate, its folder.

Usage:
    python sonic2wav.py --all
    python sonic2wav.py --all --variant amiga
    python sonic2wav.py "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"
    python sonic2wav.py --rom input/roms/sonic_rev01.bin
"""

import argparse
import os
import sys

from rich import box
from rich.padding import Padding
from rich.table import Table

from core.config import find_settings, load_settings
from core.ui import LABEL_W as _LABEL_W
from core.ui import branding, cli_console, error_printer, row_printer
from core.version import get_version as _get_version
from sfx.batch import asm_sources, discover, render_all, rom_sources
from sfx.config import DEFAULT_CONFIG, LEVEL_GLOBAL, RATE_AUTO, RATE_NATIVE, load_sfx_config
from sfx.export import export_all, write_exports
from sfx.slots import channel_name

console = cli_console()

_row   = row_printer(console)
_error = error_printer(console)


def _channel_summary(channels) -> str:
    return " ".join(channel_name(hw) for _, hw in channels)


def _results_table(exports, paula: bool, volumes: bool):
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                header_style="bold dim", padding=(0, 2, 0, 0))
    tbl.add_column("SFX")
    tbl.add_column("Channels")
    tbl.add_column("Ticks", justify="right")
    tbl.add_column("Length", justify="right")
    tbl.add_column("Rate", justify="right")
    if paula:
        tbl.add_column("Note", justify="center")
    if volumes:
        tbl.add_column("Vol", justify="right")
    tbl.add_column("Peak", justify="right")
    tbl.add_column("Size", justify="right")
    clips = any(e.clipped for e in exports)
    if clips:
        tbl.add_column("Clipped", justify="right")
    for e in exports:
        note = [e.note] if paula else []
        volume = [str(e.volume)] if volumes else []
        tbl.add_row(
            e.name,
            _channel_summary(e.render.channels),
            str(e.render.ticks),
            f"{e.seconds:.2f}s",
            f"{e.rate} Hz",
            *note,
            *volume,
            f"{e.peak_dbfs:+.1f} dB",
            f"{e.size / 1024:.1f}K",
            *([str(e.clipped or "")] if clips else []),
        )
    return tbl


def _render_label(cfg) -> tuple[str, str]:
    """The Render row: what is written, then how it is shaped."""
    out = cfg.output
    if out.rate == RATE_NATIVE:
        rate = "native rate"
    elif out.rate == RATE_AUTO:
        rate = f"auto rate ≤ {out.max_rate:g} Hz"
    else:
        rate = f"{out.rate} Hz"
    fold = f" ({out.mono_fold} fold)" if not out.stereo else ""
    what = f"{cfg.render.fps:g} Hz tick · {rate} · {out.bits}-bit {out.channels}{fold} {out.format}"

    boost = f" × {out.baked:.4g}, volume {out.volume}" if out.boost != 1.0 else ""
    level = f"one gain, peak {out.peak_dbfs:+.1f} dBFS{boost}" if out.level == LEVEL_GLOBAL else "per-sample normalise"
    how = [level]
    if out.dc_block:
        how.append("DC removed")
    if out.bits == 8:
        how.append(f"dither {out.dither}")
    if out.paula:
        how.append("ProTracker periods")
    return what, " · ".join(how)


def _sources(args, cfg):
    """(sources, what the report calls them): a ROM's, --all's folder, or the files named."""
    if args.rom:
        try:
            return rom_sources(args.rom), args.rom
        except (OSError, ValueError) as e:
            _error(str(e))

    if args.all:
        try:
            paths = discover(cfg.input)
        except FileNotFoundError as e:
            _error(str(e))
        if not paths:
            _error(f"No 'Snd*.asm' files found in {cfg.input}")
        return asm_sources(paths), cfg.input

    missing = [p for p in args.files if not os.path.isfile(p)]
    if missing:
        _error("File not found: " + ", ".join(missing))
    return asm_sources(args.files), f"{len(args.files)} file(s)"


def main():
    parser = argparse.ArgumentParser(
        description="Render Sonic 1 SMPS sound effects as configs/sfx.yaml says (WAV or raw, 8 or 16 bits)"
    )
    parser.add_argument('files', nargs='*',
                        help="SFX .asm files to render (default: use --all)")
    parser.add_argument('--all', '-a', action='store_true',
                        help="Render every SFX in sfx.yaml's input folder")
    parser.add_argument('--rom', metavar='FILE',
                        help="Render every SFX in a Sonic 1 ROM (.bin), with its known data fixes")
    parser.add_argument('--config', '-c', default=str(DEFAULT_CONFIG),
                        help="SFX settings (default: configs/sfx.yaml)")
    parser.add_argument('--variant', metavar='NAME',
                        help="read sfx.yaml as its variant NAME: each `variants: {NAME: ...}` block laid "
                             "over the keys beside it (amiga: Paula's 8-bit raw samples)")
    parser.add_argument('--out', '-o', default=None,
                        help="Output directory (default: sfx.yaml output.directory)")
    parser.add_argument('--dry-run', action='store_true',
                        help="Render and report, but write no files")
    parser.add_argument('--version', action='version', version=f"sonic2wav {_get_version()}")
    args = parser.parse_args()

    branding(console, "SONIC2WAV", _get_version())

    if not args.files and not args.all and not args.rom:
        parser.print_help()
        sys.exit(1)

    # sfx.yaml: what to render and write; settings.yaml: the chip clocks, the Amiga clock the
    # periods follow, the resampler's width
    try:
        cfg = load_sfx_config(args.config, args.variant)
    except (OSError, ValueError) as e:
        _error(str(e))
    synth, psg_synth = load_settings(find_settings())
    out_dir = args.out or cfg.output.directory

    sources, source = _sources(args, cfg)

    console.rule("[dim]Sonic 1 sound effects[/dim]")
    console.print()
    _row("Input", source, f"{len(sources)} SFX")
    _row("Config", args.config + (f" · variant {args.variant}" if args.variant else ""))
    _row("Render", *_render_label(cfg))

    # Render at the chip's rate, then shape, level and quantise the set
    with console.status("[dim]Rendering…[/dim]", spinner="dots"):
        try:
            renders = render_all(
                sources,
                fps=cfg.render.fps,
                tail_secs=cfg.render.tail_seconds,
                max_secs=cfg.render.max_seconds,
                psg_gain=cfg.render.psg_gain,
                psg_oob=cfg.render.psg_past_table,
                fm_clock=synth.clock_rate,
                psg_clock=psg_synth.clock_rate,
                psg_oversample=psg_synth.psg_oversample,
                taps=synth.resample_taps,
            )
        except (ValueError, FileNotFoundError) as e:
            _error(str(e))
        exports = export_all(renders, cfg.output, taps=synth.resample_taps, clock=synth.amiga_clock)

    console.print()
    console.print(Padding(_results_table(exports, cfg.output.paula, cfg.output.level != LEVEL_GLOBAL), (0, 0, 0, _LABEL_W + 4)))
    console.print()

    total = f"{len(exports)} files · {sum(e.seconds for e in exports):.1f}s · " \
            f"{sum(e.size for e in exports) / 1024:.0f} KB"
    if args.dry_run:
        _row("Output", "[yellow]dry run — nothing written[/yellow]", f"would write {total}")
    else:
        written = write_exports(exports, cfg.output, out_dir, clock=synth.amiga_clock)
        manifest = " · manifest.yaml" if len(written) > len(exports) else ""
        _row("Output", out_dir, total + manifest)

    warned = [r for r in renders if r.warnings]
    if warned:
        console.print()
        console.rule("[dim]Warnings[/dim]")
        console.print()
        for r in warned:
            for w in r.warnings:
                _row("Note", f"[yellow]{r.name}[/yellow] — {w}")
    console.print()


if __name__ == '__main__':
    main()
