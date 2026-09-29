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
    python tools/mod_audit.py output/02_ghz_lofi_merged.mod --banks    # each bank sound (9xx offset) too

What each sample costs against what it earns: `KB%` is its share of the file's sample bytes,
`play%` the share of the song's time it sounds on some channel (summed over channels, so two
at once count twice), `range` the notes it plays and the lowest playback rate.  The song is
walked in play order once (Bxx / Dxx followed, stopping where it loops).
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
LOW_RATE_HZ = 5000.0        # below this a sample has under 2.5 kHz of bandwidth
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
            "finetune": ((d[o + 24] & 0x0F) ^ 8) - 8,
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
    for s in samples:
        s["data"] = d[off:off + s["bytes"]]
        off += s["bytes"]
    return {"tag": tag, "channels": channels, "samples": samples, "order": order, "patterns": patterns}


DUP_WINDOW_SECS = 0.1      # how much of the attack two samples must share to be one sound
DUP_CORRELATION = 0.98


def _signed(data: bytes) -> list[int]:
    return [b - 256 if b > 127 else b for b in data]


def _corr(x: list[int], y: list[int]) -> tuple[float, float]:
    """(correlation, rms(x) / rms(y)) of two equal-length signals; (0, 0) for silence."""
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= n or syy <= n:          # an rms under 1 LSB: nothing to compare
        return 0.0, 0.0
    return sxy / (sxx * syy) ** 0.5, (sxx / syy) ** 0.5


def duplicates(samples: list[dict], rates: dict[int, float], skip: set[int]) -> dict[int, str]:
    """{slot: flag} for every sample whose attack is another slot's waveform (DUP_WINDOW_SECS
    at the rate it plays at, correlation >= DUP_CORRELATION).  The bytes are compared as they
    are: a mix made the same distance below its trigger note as another holds the same
    waveform, whatever pitch each is played at.  The flag names the earlier slot, the level
    difference (sample volume and bytes together) and, where they part within the shorter
    sample, how long they agree."""
    out: dict[int, str] = {}
    sig = {i: _signed(s["data"]) for i, s in enumerate(samples, 1) if i not in skip and s["bytes"] > 256}
    for b in sorted(sig):
        for a in sorted(sig):
            if a >= b:
                break
            rate = rates.get(a) or rates.get(b) or 8363.0
            n = min(len(sig[a]), len(sig[b]), max(256, round(rate * DUP_WINDOW_SECS)))
            c, ratio = _corr(sig[b][:n], sig[a][:n])
            if c < DUP_CORRELATION:
                continue
            va, vb = samples[a - 1]["volume"], samples[b - 1]["volume"]
            db = 20 * __import__("math").log10(max(1e-9, ratio * vb / max(va, 1)))
            # Where they part: the first 50 ms window whose correlation drops under 0.9
            w = max(64, round(rate * 0.05))
            short = min(len(sig[a]), len(sig[b]))
            part = next((k for k in range(0, short - w + 1, w)
                         if _corr(sig[b][k:k + w], sig[a][k:k + w])[0] < 0.9), None)
            if part is not None:
                span = f" for {part / rate * 1000:.0f} ms"
            elif abs(len(sig[a]) - len(sig[b])) > w:
                span = f" for all {short / rate * 1000:.0f} ms of the shorter"
            else:
                span = ""
            fa, fb = samples[a - 1]["finetune"], samples[b - 1]["finetune"]
            if fa != fb:
                out[b] = f"finetune variant of {a} ({fb - fa:+d}{span}, {db:+.1f} dB)"
            else:
                out[b] = f"same as {a}{span} ({db:+.1f} dB)"
            break
    return out


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
    offsets: dict[int, list[tuple[int, int, float]]] = defaultdict(list)  # bank inst -> [(9xx offset, period, secs)]
    banked: set[int] = set()                                             # instruments started with 9xx
    per_chan_last: dict[int, tuple[int, int, float, int] | None] = {c: None for c in range(chans)}
    flat = 0
    for _pat, cells in _play_order(mod):
        for c, (period, inst, eff, par) in enumerate(cells):
            sounding = per_chan_last[c]
            ends = None
            if period and inst:
                ends = flat + (par & 0xF) / speed if eff == 0xE and (par >> 4) == 0xD else flat
            elif eff == 0xC and par == 0:
                ends = flat
            elif eff == 0xE and (par >> 4) == 0xC:
                ends = flat + (par & 0xF) / speed
            if ends is not None and sounding is not None:
                i, p, start, off = sounding
                plays[i].append((c, p, (ends - start) * row_secs))
                if off >= 0:
                    offsets[i].append((off, p, (ends - start) * row_secs))
                per_chan_last[c] = None
            if period and inst:
                off = par if eff == 0x9 else (0 if inst in banked else -1)
                per_chan_last[c] = (inst, period, flat, off)
                if eff == 0x9:
                    banked.add(inst)
        flat += 1
    for c, sounding in per_chan_last.items():
        if sounding is not None:
            i, p, start, off = sounding
            plays[i].append((c, p, (flat - start) * row_secs))
            if off >= 0:
                offsets[i].append((off, p, (flat - start) * row_secs))
    song_secs = flat * row_secs
    sample_bytes = sum(s["bytes"] for s in mod["samples"]) or 1
    rows_out: list[dict] = []
    notes_out: list[str] = []
    rates = {}
    for i in plays:
        periods = Counter(p for _c, p, _s in plays[i])
        rates[i] = AMIGA_CLOCK / periods.most_common(1)[0][0]
    dups = duplicates(mod["samples"], rates, banked)
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
        if i in dups:
            flags.append(dups[i])

        # Time it sounds: each note until its bytes run out (a looped sample: the whole note)
        heard = sum(sec if looped or not s["bytes"] else min(sec, s["bytes"] * p / AMIGA_CLOCK)
                    for _c, p, sec in notes) if i not in banked else sum(sec for _c, _p, sec in notes)
        lo = max((p for _c, p, _s in notes), default=None)      # the longest period: the lowest note
        hi = min((p for _c, p, _s in notes), default=None)
        if lo is not None and AMIGA_CLOCK / lo < LOW_RATE_HZ:
            flags.append(f"low rate ({AMIGA_CLOCK / lo:.0f} Hz at {note_name(lo)})")
        rows_out.append({
            "inst": i, "name": s["name"], "bytes": s["bytes"], "volume": s["volume"],
            "secs": secs, "note": note_name(top) if top else "-", "loop": (s["loop_start"], s["loop_len"]) if looped else None,
            "notes": len(notes), "channels": sorted({c + 1 for c, _p, _s in notes}), "longest": longest, "flags": flags,
            "kb_share": s["bytes"] / sample_bytes, "play_share": heard / song_secs if song_secs else 0.0,
            "range": (note_name(lo), note_name(hi), AMIGA_CLOCK / lo) if lo is not None and hi is not None else None,
            "sounds": _bank_sounds(offsets.get(i, []), s["bytes"]) if i in banked else [],
        })
    used_cols = sorted({c for pos in mod["order"] for row in pats[pos] for c, cell in enumerate(row) if any(cell)})
    notes_out.append(f"{mod['tag']} ({chans} channels), columns with anything in them: {[c + 1 for c in used_cols]}")
    total = sum(s["bytes"] for s in mod["samples"])
    dead = sum(r["bytes"] for r in rows_out if "unused" in r["flags"])
    notes_out.append(f"samples: {total / 1024:.0f} KB in {sum(1 for s in mod['samples'] if s['bytes'])} slots; "
                     f"{dead / 1024:.0f} KB unused; speed {speed}, {bpm} BPM ({row_secs * 1000:.0f} ms a row); "
                     f"{song_secs:.1f} s to the loop")
    return rows_out, notes_out


def _play_order(mod: dict):
    """(pattern, cells) of every row in play order, once: Bxx and Dxx followed, stopping where
    the song loops back to a row already played."""
    order, pats = mod["order"], mod["patterns"]
    seen: set[tuple[int, int]] = set()
    pos, row = 0, 0
    while pos < len(order) and (pos, row) not in seen:
        seen.add((pos, row))
        cells = pats[order[pos]][row]
        yield order[pos], cells
        jump = None
        for _p, _i, eff, par in cells:
            if eff == 0xB:
                jump = (par, 0)
            elif eff == 0xD:
                jump = (pos + 1, (par >> 4) * 10 + (par & 0xF))
        if jump is not None:
            pos, row = jump
        elif row == 63:
            pos, row = pos + 1, 0
        else:
            row += 1


def _bank_sounds(hits: list[tuple[int, int, float]], size: int) -> list[dict]:
    """Per sound of a bank (its 9xx offset): bytes to the next sound, notes, seconds heard."""
    starts = sorted({off for off, _p, _s in hits})
    out = []
    for k, off in enumerate(starts):
        end = starts[k + 1] * 256 if k + 1 < len(starts) else size
        mine = [(p, s) for o, p, s in hits if o == off]
        out.append({"offset": off, "bytes": end - off * 256, "notes": len(mine), "secs": sum(s for _p, s in mine)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("mod")
    ap.add_argument("--slack", type=float, default=2.0, help="seconds past the longest note before a sample is oversize (default 2)")
    ap.add_argument("--banks", action="store_true", help="list each bank sound (9xx offset): bytes, notes, seconds heard")
    args = ap.parse_args()
    rows, notes = audit(args.mod, args.slack)
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
