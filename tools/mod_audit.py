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

    python tools/mod_audit.py output/02_green_hill_zone_merged.mod
    python tools/mod_audit.py output/02_green_hill_zone_merged.mod --slack 1.5   # oversize = 1.5 s past the longest note
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.tables import PERIOD_TABLE

AMIGA_CLOCK = 3546895.0
NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]


def note_name(period: int) -> str:
    """The MOD note a period sounds, in FT2's spelling (octaves 1-3 → 2-4 as FT2 shows them)."""
    if period in PERIOD_TABLE:
        i = PERIOD_TABLE.index(period)
        return f"{NOTE_NAMES[i % 12]}{i // 12 + 2}"
    return f"p{period}"


def read_mod(path: str) -> dict:
    d = Path(path).read_bytes()
    tag = d[1080:1084].decode("latin1")
    channels = {"M.K.": 4, "M!K!": 4, "4CHN": 4, "6CHN": 6, "8CHN": 8, "10CH": 10, "12CH": 12, "16CH": 16}.get(tag)
    if channels is None:
        raise ValueError(f"{path}: unknown format tag {tag!r}")
    samples = []
    for i in range(31):
        o = 20 + i * 30
        samples.append({
            "name": d[o:o + 22].rstrip(b"\0").decode("latin1"),
            "bytes": struct.unpack(">H", d[o + 22:o + 24])[0] * 2,
            "volume": d[o + 25],
            "loop_start": struct.unpack(">H", d[o + 26:o + 28])[0] * 2,
            "loop_len": struct.unpack(">H", d[o + 28:o + 30])[0] * 2,
        })
    n_pos = d[950]
    order = list(d[952:952 + n_pos])
    n_pat = max(d[952:952 + 128]) + 1
    off = 1084
    patterns = []
    for _ in range(n_pat):
        rows = []
        for _r in range(64):
            cells = []
            for _c in range(channels):
                b0, b1, b2, b3 = d[off:off + 4]
                cells.append((((b0 & 0x0F) << 8) | b1, (b0 & 0xF0) | (b2 >> 4), b2 & 0x0F, b3))
                off += 4
            rows.append(cells)
        patterns.append(rows)
    return {"tag": tag, "channels": channels, "samples": samples, "order": order, "patterns": patterns}


def audit(path: str, slack: float = 2.0) -> tuple[list[dict], list[str]]:
    mod = read_mod(path)
    pats, chans = mod["patterns"], mod["channels"]
    # Tempo: speed and BPM from F commands as they occur in play order (row 0 defaults)
    speed, bpm = 6, 125
    for cell in pats[mod["order"][0]][0] if mod["order"] else []:
        if cell[2] == 0xF:
            if cell[3] < 32:
                speed = cell[3] or speed
            else:
                bpm = cell[3]
    row_secs = speed * 2.5 / bpm
    # Every note-on in play order, with how long it sounds: to the channel's next note-on, C00
    # or ECx.  A looped sample sounds the whole way; an unlooped one until its bytes run out.
    plays: dict[int, list[tuple[int, int, float]]] = defaultdict(list)   # inst -> [(channel, period, secs)]
    banked: set[int] = set()                                             # instruments started with 9xx
    per_chan_last: dict[int, tuple[int, int, int] | None] = {c: None for c in range(chans)}   # (inst, period, start row)
    flat = 0
    for pos in mod["order"]:
        for row in pats[pos]:
            for c, (period, inst, eff, par) in enumerate(row):
                sounding = per_chan_last[c]
                ends = None
                if period and inst:
                    ends = flat + (par & 0xF) / speed if eff == 0xE and (par >> 4) == 0xD else flat
                elif eff == 0xC and par == 0:
                    ends = flat
                elif eff == 0xE and (par >> 4) == 0xC:
                    ends = flat + (par & 0xF) / speed
                if ends is not None and sounding is not None:
                    i, p, start = sounding
                    plays[i].append((c, p, (ends - start) * row_secs))
                    per_chan_last[c] = None
                if period and inst:
                    per_chan_last[c] = (inst, period, flat)
                    if eff == 0x9:
                        banked.add(inst)
            flat += 1
    for c, sounding in per_chan_last.items():
        if sounding is not None:
            i, p, start = sounding
            plays[i].append((c, p, (flat - start) * row_secs))
    rows_out: list[dict] = []
    notes_out: list[str] = []
    for i, s in enumerate(mod["samples"], 1):
        notes = plays.get(i, [])
        if not notes and not s["bytes"]:
            continue
        periods = Counter(p for _c, p, _s in notes)
        top = periods.most_common(1)[0][0] if periods else None
        rate = AMIGA_CLOCK / top if top else None
        secs = s["bytes"] / rate if rate else None
        looped = s["loop_len"] > 2
        longest = max((sec for _c, _p, sec in notes), default=0.0)
        flags = []
        if not notes:
            flags.append("unused")
        elif not s["bytes"]:
            flags.append("empty slot")
        elif i in banked:
            flags.append("sample bank (9xx offsets)")
        elif rate:
            if not looped and secs is not None and secs + 0.05 < longest:
                flags.append(f"too short by {longest - secs:.2f} s")
            if not looped and secs is not None and secs > longest + slack:
                flags.append(f"oversize by {secs - longest:.1f} s")
            if looped and (s["loop_start"] / rate) > longest + slack:
                flags.append(f"loop starts {s['loop_start'] / rate - longest:.1f} s past the longest note")
        rows_out.append({
            "inst": i, "name": s["name"], "bytes": s["bytes"], "volume": s["volume"],
            "secs": secs, "note": note_name(top) if top else "-", "loop": (s["loop_start"], s["loop_len"]) if looped else None,
            "notes": len(notes), "channels": sorted({c + 1 for c, _p, _s in notes}), "longest": longest, "flags": flags,
        })
    used_cols = sorted({c for pos in mod["order"] for row in pats[pos] for c, cell in enumerate(row) if any(cell)})
    notes_out.append(f"{mod['tag']} ({chans} channels), columns with anything in them: {[c + 1 for c in used_cols]}")
    total = sum(s["bytes"] for s in mod["samples"])
    dead = sum(r["bytes"] for r in rows_out if "unused" in r["flags"])
    notes_out.append(f"samples: {total / 1024:.0f} KB in {sum(1 for s in mod['samples'] if s['bytes'])} slots; "
                     f"{dead / 1024:.0f} KB unused; speed {speed}, {bpm} BPM ({row_secs * 1000:.0f} ms a row)")
    return rows_out, notes_out


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("mod")
    ap.add_argument("--slack", type=float, default=2.0, help="seconds past the longest note before a sample is oversize (default 2)")
    args = ap.parse_args()
    rows, notes = audit(args.mod, args.slack)
    for n in notes:
        print(n)
    print()
    head = f"{'inst':>4} {'name':21} {'bytes':>6} {'secs':>5} {'at':>4} {'loop':>13} {'notes':>5} {'longest':>7} {'chans':8} flags"
    print(head)
    print("-" * len(head))
    for r in rows:
        loop = f"{r['loop'][0]}+{r['loop'][1]}" if r["loop"] else "-"
        secs = f"{r['secs']:.2f}" if r["secs"] is not None else "-"
        print(f"{r['inst']:>4} {r['name'][:21]:21} {r['bytes']:>6} {secs:>5} {r['note']:>4} {loop:>13} {r['notes']:>5} "
              f"{r['longest']:>7.2f} {','.join(map(str, r['channels'])):8} {'; '.join(r['flags'])}")


if __name__ == "__main__":
    main()
