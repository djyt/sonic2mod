#!/usr/bin/env python3
"""Audit a MOD's samples against the notes that play them: length, loops, waste, silence.

Reads the file alone (no config), so it audits any MOD the converter wrote — the merged
build above all.  For every instrument it reports the sample's bytes and seconds at the note
it is played at most, its loop, how many notes play it on which channels, the longest note
(from the note-on to the channel's next note-on or cut, at the MOD's own tempo), and flags:

    unused        no note plays it (its bytes are dead weight)
    too short     an unlooped sample shorter than a note that plays it: the note falls silent
    oversize      an unlooped sample far longer than its longest note (the tail is never heard);
                  a looped one whose loop starts far past the longest note
    empty slot    a note plays an instrument with no sample: silence
    low rate      a note plays it below LOW_RATE_HZ: the Amiga's output filter and the missing
                  bandwidth make it dull (a sample made for one pitch, played an octave down)
    same as N     its waveform matches slot N's over the first 100 ms (correlation >= 0.98, the
                  level difference shown; "for X ms" where the two part later, "all of the
                  shorter" where one is the other's start): one sound in two slots - a composite
                  that is its primary louder, or two of one chord shape
    finetune variant of N   the same, at another finetune (a chorus detune: intended)

    python tools/mod_audit.py output/02_green_hill_zone_merged.mod
    python tools/mod_audit.py output/02_green_hill_zone_merged.mod --slack 1.5   # oversize = 1.5 s past the longest note
    python tools/mod_audit.py output/02_green_hill_zone_lofi_merged.mod --banks    # each bank sound (9xx offset) too

What each sample costs against what it earns: `KB%` is its share of the file's sample bytes,
`play%` the share of the song's time it sounds on some channel (summed over channels, so two
at once count twice), `range` the notes it plays and the lowest playback rate.  The song is
walked in play order once (Bxx / Dxx followed, stopping where it loops).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import find_settings, load_settings
from core.mod import audit


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("mod")
    ap.add_argument("--slack", type=float, default=2.0, help="seconds past the longest note before a sample is oversize (default 2)")
    ap.add_argument("--banks", action="store_true", help="list each bank sound (9xx offset): bytes, notes, seconds heard")
    args = ap.parse_args()
    synth, _psg = load_settings(find_settings())        # settings.yaml amiga_clock: the rate a period plays at
    rows, notes = audit(args.mod, args.slack, synth.amiga_clock)
    for n in notes:
        print(n)
    print()
    head = (f"{'inst':>4} {'name':21} {'bytes':>6} {'KB%':>4} {'secs':>5} {'at':>4} {'loop':>13} {'notes':>5} "
            f"{'play%':>5} {'longest':>7} {'range':>16} {'chans':8} flags")
    print(head)
    print("-" * len(head))
    for r in rows:
        loop = f"{r['loop'][0]}+{r['loop'][1]}" if r["loop"] else "-"
        secs = f"{r['secs']:.2f}" if r["secs"] is not None else "-"
        rng = f"{r['range'][0]}-{r['range'][1]} {r['range'][2] / 1000:.1f}k" if r["range"] else "-"
        print(f"{r['inst']:>4} {r['name'][:21]:21} {r['bytes']:>6} {r['kb_share'] * 100:>4.1f} {secs:>5} {r['note']:>4} "
              f"{loop:>13} {r['notes']:>5} {r['play_share'] * 100:>5.1f} {r['longest']:>7.2f} {rng:>16} "
              f"{','.join(map(str, r['channels'])):8} {'; '.join(r['flags'])}")
        if args.banks:
            for snd in r["sounds"]:
                print(f"{'':>4}   9{snd['offset']:02X} {snd['bytes']:>12} {'':>4} {'':>5} {'':>4} {'':>13} {snd['notes']:>5} "
                      f"{snd['secs'] / max(1e-9, sum(x['secs'] for x in r['sounds'])) * 100:>5.1f}")


if __name__ == "__main__":
    main()
