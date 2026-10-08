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

Usage::

    python tools/vgm_pitch_audit.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz"
    python tools/vgm_pitch_audit.py cfg.yaml ref.vgz --mod output/x.mod --min-ms 40 --list
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audit import audit_pitches, mod_pitch_timeline, note_start_offset, prepare_audit
from core.mod import read_mod
from core.plan import load_config
from core.ui import add_variant_argument, print_audit
from core.vgm import pitch_segments, read_vgm


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="song YAML config")
    ap.add_argument("vgz", help="reference VGM/VGZ recording of the same song")
    ap.add_argument("--mod", help="MOD to audit (default: config output_file)")
    ap.add_argument("--offset", type=float, default=None,
                    help="seconds the MOD lags the VGM (default: found by matching note starts)")
    ap.add_argument("--min-ms", type=float, default=60.0,
                    help="ignore chip segments shorter than this (grace notes, vibrato steps; default 60)")
    ap.add_argument("--tolerance", type=float, default=35.0, help="cents before a note counts as wrong (default 35)")
    ap.add_argument("--list", action="store_true", help="print every wrong / missing segment with its time")
    ap.add_argument("--json", metavar="FILE", help="write the alignment and the per-instrument verdicts as JSON")
    add_variant_argument(ap)
    ap.add_argument("--settings", metavar="PATH",
                    help="settings the MOD was converted with (default: settings.yaml beside the config, "
                         "else configs/settings.yaml): whether it has detune variants")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    cfg = load_config(args.config, args.settings, variant=args.variant)
    song = prepare_audit(cfg, args.settings, args.config)       # synth roots and detune variants, as the converter
    mod_path = Path(args.mod or cfg.output_file)
    chip, vgm_end = pitch_segments(read_vgm(args.vgz))
    mod, mod_end = mod_pitch_timeline(read_mod(mod_path), cfg, song)
    chan_map = {c.source: c.mod_channel for c in cfg.channels}
    if args.offset is None:
        args.offset = note_start_offset(chip, mod, chan_map)
        how = "auto"
    else:
        how = "given"
    print(f"VGM {vgm_end:.1f} s   MOD one pass {mod_end:.1f} s   MOD lags by {args.offset * 1000:+.0f} ms ({how})   ({mod_path})")
    print(f"chip segments >= {args.min_ms:g} ms; wrong = more than {args.tolerance:g} cents from the chip")
    print()

    res = audit_pitches(chip, vgm_end, mod, mod_end, chan_map, args.offset, args.min_ms, args.tolerance)
    print_audit(res, args.min_ms, listing=args.list)
    if args.json:
        Path(args.json).write_text(json.dumps({"offset_s": args.offset, "instruments": res["instruments"]}, indent=2) + "\n",
                                   encoding="utf-8")
    sys.exit(1 if res["bad"] else 0)


if __name__ == "__main__":
    main()
