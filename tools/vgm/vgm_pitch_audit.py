#!/usr/bin/env python3
"""Symbolic pitch audit of a converted MOD against its VGM/VGZ reference.

No audio is rendered.  The chip side is the frequency-register timeline of the recording
(every YM2612 $A4/$A0 write and key on/off, every SN76489 tone/volume write), so pitch changes
under smpsNoAttack are seen as well as key-ons.  The MOD side is the pitch each pattern note
sounds at, computed from the config: a note at MOD index n on an instrument synthesised at
``synth_root`` and anchored at ``root`` sounds at

    f = 440 * 2^((synth_root - 57) / 12) * period[root] / period[n]      (* 2^(finetune / 96))

For every chip segment longer than --min-ms the MOD note sounding at its midpoint is looked up
and the two pitches are compared.  Use this for "is every note right"; use vgm_compare.py for
levels, timing, timbre and vibrato.  Its per-note pitch column measures audio windows and is
unreliable on legato runs and 1-tick grace notes (GHZ FM3-FM5), which this tool is immune to.

Both sides are in real Hz: FM frequencies come from core.vgm.fm_frequency_hz and a
``synth_root`` name is the pitch the synthesiser actually renders (freq_to_fnum_block).

The rip is the config's (rips.yaml beside it, or the one sharing its number) unless named.  The
MOD audited is the one on disk: --convert writes it first.  --trace shows why a note was judged
as it was: each chip segment near a time, the MOD note it paired with (or looked up), the running
MOD-minus-chip deviation.  --slides judges slides too (Time Trax's glides: a pitch each frame, every
segment shorter than --min-ms): each step against the MOD's 1xx / 2xx at its midpoint.

Usage::

    python tools/vgm/vgm_pitch_audit.py configs/sonic_1/02_green_hill_zone.yaml "reference/vgz/sonic_1/02 - Green Hill Zone.vgz"
    python tools/vgm/vgm_pitch_audit.py configs/streets_of_rage_2/11_under_logic.yaml --list      # its rip from rips.yaml
    python tools/vgm/vgm_pitch_audit.py cfg.yaml ref.vgz --mod output/x.mod --min-ms 40 --list
    python tools/vgm/vgm_pitch_audit.py --all --configs configs/streets_of_rage_2 --convert       # every pair, a line each
    python tools/vgm/vgm_pitch_audit.py cfg.yaml --trace PSG2 --at 29.9                           # how 29.4-30.4 s was paired
    python tools/vgm/vgm_pitch_audit.py cfg.yaml --slides --list                                   # glides judged too
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(ROOT))

from core.audio import pitch_name
from core.audit import PairStep, audit_pitches, mod_pitch_timeline, named, note_start_offset, rip_on_song_clock, workers
from core.config import ConversionConfig
from core.convert import prepare_conversion
from core.mod import read_mod
from core.plan import prepare_instruments
from core.ui import add_shelf_arguments, add_variant_argument, print_audit, rip_shelf
from core.vgm import decode_vgm, pitch_segments

_TRACE_SPAN = 0.5                 # s either side of --at a trace shows
_WORST_CHANNELS = 4               # channels a --all line names
_SLIDE_CENTS = 50.0               # --slides: cents a slide's step may sit off the MOD's (a row's slide meets the
                                  # chip only at the row's end)


@dataclass(frozen=True)
class _Options:
    mod: str | None = None
    offset: float | None = None
    min_ms: float = 60.0
    tolerance: float = 35.0
    variant: str | None = None
    settings: str | None = None
    convert: bool = False
    trace: tuple[str, float] | None = None      # (channel, seconds)
    slides: float | None = None                 # cents a slide's step may be off; None: slides not judged


@dataclass
class _Audit:
    title: str
    header: list[str] = field(default_factory=list)
    res: dict | None = None
    offset: float = 0.0
    steps: list[PairStep] = field(default_factory=list)
    error: str = ""


def audit(config: Path, rip: Path, opt: _Options) -> _Audit:
    """One config's MOD against `rip` (a worker's job)."""
    out = _Audit(config.stem)
    if opt.convert:
        done = _convert(config, opt)
        if done:
            out.error = done
            return out
    try:
        prep = prepare_conversion(ConversionConfig.from_yaml(str(config), opt.variant), str(config), opt.settings)
    except ValueError as e:
        out.error = str(e)
        return out
    cfg, song = prep.config, prep.song
    conv = prepare_instruments(song, cfg, prep.synth).conv     # synth roots and detune variants, as the converter
    mod_path = Path(opt.mod or cfg.output_file)
    if not mod_path.exists():
        out.error = f"no MOD at {mod_path} (--convert)"
        return out

    # Both timelines; a rip whose timer ran off its driver's retimed onto it
    played, scale = rip_on_song_clock(str(rip), song, cfg.region)
    if scale != 1.0:
        out.header.append(f"VGM retimed x{scale:.5f} onto the driver's clock (its frames {scale - 1:+.2%} off)")
    chip, vgm_end = pitch_segments(decode_vgm(played))
    mod, mod_end = mod_pitch_timeline(read_mod(mod_path), conv, song)
    chan_map = {c.source: c.mod_channel for c in cfg.channels}
    out.offset = note_start_offset(chip, mod, chan_map) if opt.offset is None else opt.offset
    how = "auto" if opt.offset is None else "given"
    out.header.append(f"VGM {vgm_end:.1f} s   MOD one pass {mod_end:.1f} s   MOD lags by {out.offset * 1000:+.0f} ms ({how})"
                      f"   ({mod_path})")
    out.header.append(f"chip segments >= {opt.min_ms:g} ms; wrong = more than {opt.tolerance:g} cents from the chip")
    if opt.slides is not None:
        out.header.append(f"slides: runs of shorter segments, off = more than {opt.slides:g} cents from the MOD's slide")

    out.res = audit_pitches(chip, vgm_end, mod, mod_end, chan_map, out.offset, opt.min_ms, opt.tolerance,
                            trace=_tracer(opt.trace, out.steps) if opt.trace else None, slide_tolerance=opt.slides)
    return out


def _convert(config: Path, opt: _Options) -> str:
    """convert.py on `config`, as the MOD was meant to be written; '' or why it failed."""
    argv = [sys.executable, str(ROOT / "convert.py"), str(config)]
    argv += ["--variant", opt.variant] if opt.variant else []
    argv += ["--settings", opt.settings] if opt.settings else []
    done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return "" if done.returncode == 0 else f"convert.py failed (exit {done.returncode})"


def _tracer(trace: tuple[str, float], steps: list[PairStep]):
    channel, at = trace

    def keep(step: PairStep) -> None:
        if step.channel == channel and abs(step.start - at) <= _TRACE_SPAN:
            steps.append(step)

    return keep


# --- output ------------------------------------------------------------------------

def _print_trace(steps: list[PairStep]) -> None:
    """Each chip segment near the time: its start, pitch, how it was paired, what it was judged against."""
    print(f"{'chip s':>9} {'ms':>5}  {'pitch':<5} {'start':<5} {'dev ms':>7}  {'paired':>8}  {'MOD':<5} {'inst':>4}  verdict")
    for s in steps:
        paired = f"{s.paired:8.3f}" if s.paired is not None else "  midpt "
        mod, inst = (pitch_name(s.mod[1]), s.mod[2]) if s.mod else ("-", "")
        print(f"{s.start:9.3f} {(s.end - s.start) * 1000:5.0f}  {pitch_name(s.hz):<5} {'yes' if s.note_start else '':<5} "
              f"{s.deviation * 1000:+7.0f}  {paired}  {mod:<5} {inst!s:>4}  {s.verdict}")


def _line(a: _Audit) -> str:
    """One pair: its counts and the channels with the most wrong or missing notes."""
    if a.res is None:
        return f"  {a.title:<30} not audited: {a.error}"
    chans = a.res["channels"].values()
    ok, wrong, missing = (sum(c[k] for c in chans) for k in ("ok", "wrong", "missing"))
    bad = sorted(((c["wrong"] + c["missing"], name) for name, c in a.res["channels"].items() if c["wrong"] + c["missing"]),
                 reverse=True)
    worst = ", ".join(f"{name} {n}" for n, name in bad[:_WORST_CHANNELS])
    slides = ""
    if any("slide_ok" in c for c in chans):
        slides = f"   slides ok {sum(c['slide_ok'] for c in chans):>5} off {sum(c['slide_off'] for c in chans):>4}"
    return (f"  {a.title:<30} ok {ok:>5}   wrong {wrong:>4}   missing {missing:>3}{slides}"
            + (f"   ({worst})" if worst else ""))


def _options(args: argparse.Namespace) -> _Options:
    trace = None
    if args.trace:
        if args.at is None:
            raise SystemExit("--trace needs --at SECONDS")
        trace = (args.trace, args.at)
    return _Options(args.mod, args.offset, args.min_ms, args.tolerance, args.variant, args.settings, args.convert, trace,
                    args.slides)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", help="song YAML config")
    ap.add_argument("vgz", nargs="?", help="its VGM/VGZ recording (default: the config's rip, rips.yaml)")
    ap.add_argument("--all", action="store_true", help="every pair on the shelf (--configs), a line each")
    add_shelf_arguments(ap)
    ap.add_argument("--only", nargs="+", metavar="NAME", help="with --all: rips or configs whose name holds one")
    ap.add_argument("--convert", action="store_true", help="write each MOD first (an audit reads the one on disk)")
    ap.add_argument("--mod", help="MOD to audit (default: config output_file)")
    ap.add_argument("--offset", type=float, default=None,
                    help="seconds the MOD lags the VGM (default: found by matching note starts)")
    ap.add_argument("--min-ms", type=float, default=60.0,
                    help="ignore chip segments shorter than this (grace notes, vibrato steps; default 60)")
    ap.add_argument("--tolerance", type=float, default=35.0, help="cents before a note counts as wrong (default 35)")
    ap.add_argument("--slides", type=float, nargs="?", const=_SLIDE_CENTS, metavar="CENTS",
                    help=f"judge slides too: runs of segments shorter than --min-ms, each within CENTS of the MOD's "
                         f"slide (default {_SLIDE_CENTS:g})")
    ap.add_argument("--list", action="store_true", help="print every wrong / missing segment with its time")
    ap.add_argument("--trace", metavar="CHANNEL", help="how CHANNEL's chip segments near --at were paired and judged")
    ap.add_argument("--at", type=float, metavar="SECONDS", help=f"--trace: the time (±{_TRACE_SPAN:g} s shown)")
    ap.add_argument("--json", metavar="FILE", help="write the alignment and the per-instrument verdicts as JSON")
    add_variant_argument(ap)
    ap.add_argument("--settings", metavar="PATH",
                    help="settings the MOD was converted with (default: settings.yaml beside the config, "
                         "else configs/settings.yaml): whether it has detune variants")
    args = ap.parse_args()
    if bool(args.config) == args.all:
        ap.error("name a config, or --all")
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    opt = _options(args)

    # Every pair: a line each
    if args.all:
        shelf = rip_shelf(args)
        pairs = sorted((c, r) for c, r in shelf.pairs() if named(args.only, c, r))
        if not pairs:
            raise SystemExit("no rip pairs with a config")
        with ProcessPoolExecutor(workers(len(pairs))) as pool:
            audits = list(pool.map(audit, [c for c, _ in pairs], [r for _, r in pairs], [opt] * len(pairs)))
        for a in audits:
            print(_line(a))
        clean = sum(a.res is not None and not a.res["bad"] for a in audits)
        print(f"{clean} of {len(audits)} play every note at its rip's pitch")
        sys.exit(0 if clean == len(audits) else 1)

    # One config: its rip named, or the shelf's
    config = Path(args.config)
    rip = Path(args.vgz) if args.vgz else rip_shelf(args, configs=config.parent).rip_for(config)
    if rip is None:
        raise SystemExit(f"{config}: no rip pairs with it (name one)")
    a = audit(config, rip, opt)
    if a.res is None:
        raise SystemExit(f"ERROR: {a.error}")
    for line in a.header:
        print(line)
    print()
    if opt.trace:
        _print_trace(a.steps)
        return
    print_audit(a.res, opt.min_ms, listing=args.list)
    if args.json:
        Path(args.json).write_text(json.dumps({"offset_s": a.offset, "instruments": a.res["instruments"]}, indent=2) + "\n",
                                   encoding="utf-8")
    sys.exit(1 if a.res["bad"] else 0)


if __name__ == "__main__":
    main()
