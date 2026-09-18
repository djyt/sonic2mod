#!/usr/bin/env python3
"""CLI entry point for Sonic 1 SFX-to-WAV rendering.

Runs a tick-accurate offline reimplementation of the Sonic 1 sound driver against
the YM2612 and SN76489 emulators, producing 16-bit stereo WAV files.

Usage:
    python sonic2wav.py --all
    python sonic2wav.py "sonic_1/sfx/SndB5 - Ring.asm"
    python sonic2wav.py --all --out output/sfx --rate native
"""

import argparse
import os
import sys

from rich import box
from rich.padding import Padding
from rich.table import Table

from core.cli import LABEL_W as _LABEL_W
from core.cli import branding, cli_console, error_printer, row_printer
from sfx.amiga import DEFAULT_MAX_RATE
from sfx.batch import (
    assign_volumes,
    discover,
    global_scale,
    peak_dbfs,
    prepare_8bit,
    render_all,
    write_8bit,
    write_all,
)
from sfx.render import DEFAULT_MAX_SECS, DEFAULT_TAIL_SECS, NATIVE_RATE
from sfx.resample import DEFAULT_TAPS

console = cli_console()

DEFAULT_SFX_DIR = os.path.join("sonic_1", "sfx")
DEFAULT_OUT_DIR = os.path.join("output", "sfx")
DEFAULT_OUT_DIR_8BIT = os.path.join("output", "sfx8")


from core.version import get_version as _get_version

_row   = row_printer(console)
_error = error_printer(console)


def _channel_summary(channels) -> str:
    names = {0x02: "FM3", 0x04: "FM4", 0x05: "FM5",
             0x80: "PSG1", 0xA0: "PSG2", 0xC0: "PSG3", 0xE0: "Noise"}
    return " ".join(names.get(hw, f"?{hw:02X}") for _, hw in channels)


def _results_table(renders, scale):
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                header_style="bold dim", padding=(0, 2, 0, 0))
    tbl.add_column("SFX")
    tbl.add_column("Channels")
    tbl.add_column("Ticks", justify="right")
    tbl.add_column("Length", justify="right")
    tbl.add_column("Peak", justify="right")
    for r in renders:
        tbl.add_row(
            r.name,
            _channel_summary(r.channels),
            str(r.ticks),
            f"{r.seconds:.2f}s",
            f"{peak_dbfs(r, scale):+.1f} dB",
        )
    return tbl


def _amiga_table(samples):
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                header_style="bold dim", padding=(0, 2, 0, 0))
    tbl.add_column("Sample")
    tbl.add_column("Channels")
    tbl.add_column("Rate", justify="right")
    tbl.add_column("Note", justify="center")
    tbl.add_column("Vol", justify="right")
    tbl.add_column("Size", justify="right")
    for s in samples:
        tbl.add_row(
            s.name,
            _channel_summary(s.channels),
            f"{s.rate} Hz",
            s.note,
            str(s.volume),
            f"{len(s.data) / 1024:.1f}K",
        )
    return tbl


def main():
    parser = argparse.ArgumentParser(
        description="Render Sonic 1 SMPS sound effects to 16-bit stereo WAV"
    )
    parser.add_argument('files', nargs='*',
                        help="SFX .asm files to render (default: use --all)")
    parser.add_argument('--all', '-a', action='store_true',
                        help="Render every SFX in --sfx-dir")
    parser.add_argument('--sfx-dir', default=DEFAULT_SFX_DIR,
                        help=f"Directory of SndXX .asm files (default: {DEFAULT_SFX_DIR})")
    parser.add_argument('--out', '-o', default=None,
                        help=f"Output directory (default: {DEFAULT_OUT_DIR}, "
                             f"or {DEFAULT_OUT_DIR_8BIT} with --8bit)")
    parser.add_argument('--rate', default='44100',
                        help=f"Output sample rate, or 'native' for {NATIVE_RATE} Hz "
                             "(skips resampling entirely)")
    parser.add_argument('--fps', type=float, default=60.0,
                        help="V-int rate: 60 for NTSC (default), 50 for PAL")
    parser.add_argument('--tail', type=float, default=DEFAULT_TAIL_SECS,
                        help=f"Seconds of release tail after the last event "
                             f"(default: {DEFAULT_TAIL_SECS})")
    parser.add_argument('--max-seconds', type=float, default=DEFAULT_MAX_SECS,
                        help=f"Safety cap on render length (default: {DEFAULT_MAX_SECS})")
    parser.add_argument('--psg-gain', type=float, default=1.0,
                        help="PSG level relative to FM after per-chip scaling (default: 1.0)")
    parser.add_argument('--peak-dbfs', type=float, default=-0.3,
                        help="Target peak for the loudest file (default: -0.3)")
    parser.add_argument('--no-normalize', action='store_true',
                        help="Skip global normalisation, write raw chip levels")
    parser.add_argument('--strict', action='store_true',
                        help="Clamp out-of-range PSG notes instead of extrapolating")
    parser.add_argument('--taps', type=int, default=DEFAULT_TAPS,
                        help=f"Resampler filter length (default: {DEFAULT_TAPS})")
    parser.add_argument('--dry-run', action='store_true',
                        help="Render and report, but write no files")
    parser.add_argument('--version', action='version', version=f"sonic2wav {_get_version()}")

    amiga = parser.add_argument_group(
        '8-bit Amiga export',
        'Signed 8-bit mono .raw samples plus a manifest, prepared for Paula playback'
    )
    amiga.add_argument('--8bit', dest='eightbit', action='store_true',
                       help="Export 8-bit Amiga samples instead of 16-bit WAV")
    amiga.add_argument('--max-rate', type=float, default=DEFAULT_MAX_RATE,
                       help=f"Ceiling for automatic rate selection (default: {DEFAULT_MAX_RATE}). "
                            "Use 16574 for A500 targets, where the fixed ~4.4 kHz filter "
                            "makes more pointless")
    amiga.add_argument('--flat-rate', type=float, default=None,
                       help="Force one rate for every sample instead of choosing per effect "
                            "(snapped to the nearest ProTracker period)")
    amiga.add_argument('--shape', type=int, default=1, choices=(0, 1, 2),
                       help="Dither noise shaping order (default: 1). 2 concentrates noise "
                            "higher — better at 28 kHz, worse at 8 kHz")
    amiga.add_argument('--no-dither', action='store_true',
                       help="Quantise without dither (not recommended — decay ramps go granular)")

    args = parser.parse_args()

    branding(console, "SONIC2WAV", _get_version())

    if not args.files and not args.all:
        parser.print_help()
        sys.exit(1)

    out_dir = args.out or (DEFAULT_OUT_DIR_8BIT if args.eightbit else DEFAULT_OUT_DIR)

    if args.eightbit:
        # The 8-bit path picks its own rates on the ProTracker period grid and
        # resamples once, straight from the chip rate.
        target_rate = None
    elif args.rate == 'native':
        target_rate = None
    else:
        try:
            target_rate = int(args.rate)
        except ValueError:
            _error(f"--rate must be an integer or 'native', got {args.rate!r}")

    if args.all:
        try:
            paths = discover(args.sfx_dir)
        except FileNotFoundError as e:
            _error(str(e))
        if not paths:
            _error(f"No 'Snd*.asm' files found in {args.sfx_dir}")
        source = args.sfx_dir
    else:
        paths = args.files
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            _error("File not found: " + ", ".join(missing))
        source = f"{len(paths)} file(s)"

    console.rule("[dim]Sonic 1 sound effects[/dim]")
    console.print()
    _row("Input", source, f"{len(paths)} SFX")

    if args.eightbit:
        if args.flat_rate:
            rate_label = f"flat {args.flat_rate:g} Hz"
        else:
            rate_label = f"auto rate ≤ {args.max_rate:g} Hz"
        dither_label = "no dither" if args.no_dither else f"TPDF dither, shape {args.shape}"
        _row("Render", f"{args.fps:g} Hz tick · {rate_label} · signed 8-bit mono",
             f"per-sample normalise · DC removed · {dither_label}")
    else:
        rate_label = f"{NATIVE_RATE} Hz (native)" if target_rate is None else f"{target_rate} Hz"
        _row("Render", f"{args.fps:g} Hz tick · {rate_label} · 16-bit stereo")

    renders = []
    with console.status("[dim]Rendering…[/dim]", spinner="dots"):
        try:
            renders = render_all(
                paths,
                fps=args.fps,
                tail_secs=args.tail,
                max_secs=args.max_seconds,
                psg_gain=args.psg_gain,
                psg_oob="clamp" if args.strict else "extend",
                target_rate=target_rate,
                taps=args.taps,
            )
        except (ValueError, FileNotFoundError) as e:
            _error(str(e))

    if args.eightbit:
        samples = []
        with console.status("[dim]Preparing 8-bit samples…[/dim]", spinner="dots"):
            for r in renders:
                samples.append(prepare_8bit(
                    r,
                    max_rate=args.max_rate,
                    flat_rate=args.flat_rate,
                    shape=args.shape,
                    dither=not args.no_dither,
                    taps=args.taps,
                ))
        assign_volumes(samples)

        console.print()
        console.print(Padding(_amiga_table(samples), (0, 0, 0, _LABEL_W + 4)))
        console.print()

        total_bytes = sum(len(s.data) for s in samples)
        if args.dry_run:
            _row("Output", "[yellow]dry run — nothing written[/yellow]",
                 f"would write {len(samples)} samples · {total_bytes / 1024:.0f} KB")
        else:
            written = write_8bit(samples, out_dir)
            _row("Output", out_dir,
                 f"{len(samples)} samples · {total_bytes / 1024:.0f} KB · manifest.yaml")
    else:
        scale = 1.0 if args.no_normalize else global_scale(renders, peak_dbfs=args.peak_dbfs)

        console.print()
        console.print(Padding(_results_table(renders, scale), (0, 0, 0, _LABEL_W + 4)))
        console.print()

        total_secs = sum(r.seconds for r in renders)
        if args.dry_run:
            _row("Output", "[yellow]dry run — nothing written[/yellow]")
        else:
            written = write_all(renders, out_dir, scale)
            gain_note = "raw levels" if args.no_normalize else \
                f"global scale ×{scale:.3f} → peak {args.peak_dbfs:+.1f} dBFS"
            _row("Output", out_dir,
                 f"{len(written)} files · {total_secs:.1f}s total · {gain_note}")

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
