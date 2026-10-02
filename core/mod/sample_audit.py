"""Sample audit of a written MOD: each sample against the notes that play it.

Reads the file alone (no config): bytes, the note each sample plays at most, its loop, how
many notes play it on which channels, the longest note, the share of the song it sounds and
flags (unused, too short, oversize, empty slot, low rate, same as N).  `tools/mod_audit.py` is
its command line; `convert.py` reads it for the sample table of its report.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..audio import gain_to_db
from .file import PAL_AMIGA_CLOCK, SampleInfo, read_mod
from .notes import PERIOD_TABLE

AMIGA_CLOCK = float(PAL_AMIGA_CLOCK)
LOW_RATE_HZ = 5000.0        # below this a sample has under 2.5 kHz of bandwidth
NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]


def note_name(period: int) -> str:
    """The MOD note a period sounds, in FT2's spelling (octaves 1-3 → 2-4 as FT2 shows them)."""
    if period in PERIOD_TABLE:
        i = PERIOD_TABLE.index(period)
        return f"{NOTE_NAMES[i % 12]}{i // 12 + 2}"
    return f"p{period}"


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


def duplicates(samples: list[SampleInfo], rates: dict[int, float], skip: set[int]) -> dict[int, str]:
    """{slot: flag} for every sample whose attack is another slot's waveform (DUP_WINDOW_SECS
    at the rate it plays at, correlation >= DUP_CORRELATION).  The bytes are compared as they
    are: a mix made the same distance below its trigger note as another holds the same
    waveform, whatever pitch each is played at.  The flag names the earlier slot, the level
    difference (sample volume and bytes together) and, where they part within the shorter
    sample, how long they agree."""
    out: dict[int, str] = {}
    sig = {i: _signed(s.data) for i, s in enumerate(samples, 1) if i not in skip and s.length > 256}
    for b in sorted(sig):
        for a in sorted(sig):
            if a >= b:
                break
            rate = rates.get(a) or rates.get(b) or 8363.0
            n = min(len(sig[a]), len(sig[b]), max(256, round(rate * DUP_WINDOW_SECS)))
            c, ratio = _corr(sig[b][:n], sig[a][:n])
            if c < DUP_CORRELATION:
                continue
            va, vb = samples[a - 1].volume, samples[b - 1].volume
            db = gain_to_db(max(1e-9, ratio * vb / max(va, 1)))
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
            fa, fb = samples[a - 1].finetune, samples[b - 1].finetune
            if fa != fb:
                out[b] = f"finetune variant of {a} ({fb - fa:+d}{span}, {db:+.1f} dB)"
            else:
                out[b] = f"same as {a}{span} ({db:+.1f} dB)"
            break
    return out


def audit(path: str, slack: float = 2.0) -> tuple[list[dict], list[str]]:
    mod = read_mod(path)
    pats, chans = mod.patterns, mod.channels
    # Tempo: speed and BPM from F commands as they occur in play order (row 0 defaults)
    speed, bpm = 6, 125
    for cell in pats[mod.order[0]][0] if mod.order else []:
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
    for _pat, _row, cells in mod.play_rows():
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
    sample_bytes = sum(s.length for s in mod.samples) or 1
    rows_out: list[dict] = []
    notes_out: list[str] = []
    rates = {}
    for i in plays:
        periods = Counter(p for _c, p, _s in plays[i])
        rates[i] = AMIGA_CLOCK / periods.most_common(1)[0][0]
    dups = duplicates(mod.samples, rates, banked)
    for i, s in enumerate(mod.samples, 1):
        notes = plays.get(i, [])
        if not notes and not s.length:
            continue
        periods = Counter(p for _c, p, _s in notes)
        top = periods.most_common(1)[0][0] if periods else None
        rate = AMIGA_CLOCK / top if top else None
        secs = s.length / rate if rate else None
        looped = s.looped
        longest = max((sec for _c, _p, sec in notes), default=0.0)
        flags = []
        if not notes:
            flags.append("unused")
        elif not s.length:
            flags.append("empty slot")
        elif i in banked:
            flags.append("sample bank (9xx offsets)")
        elif rate:
            if not looped and secs is not None and secs + 0.05 < longest:
                flags.append(f"too short by {longest - secs:.2f} s")
            if not looped and secs is not None and secs > longest + slack:
                flags.append(f"oversize by {secs - longest:.1f} s")
            if looped and (s.loop_start / rate) > longest + slack:
                flags.append(f"loop starts {s.loop_start / rate - longest:.1f} s past the longest note")
        if i in dups:
            flags.append(dups[i])

        # Time it sounds: each note until its bytes run out (a looped sample: the whole note)
        heard = sum(sec if looped or not s.length else min(sec, s.length * p / AMIGA_CLOCK)
                    for _c, p, sec in notes) if i not in banked else sum(sec for _c, _p, sec in notes)
        lo = max((p for _c, p, _s in notes), default=None)      # the longest period: the lowest note
        hi = min((p for _c, p, _s in notes), default=None)
        if lo is not None and AMIGA_CLOCK / lo < LOW_RATE_HZ:
            flags.append(f"low rate ({AMIGA_CLOCK / lo:.0f} Hz at {note_name(lo)})")
        rows_out.append({
            "inst": i, "name": s.name, "bytes": s.length, "volume": s.volume,
            "secs": secs, "note": note_name(top) if top else "-", "loop": (s.loop_start, s.loop_len) if looped else None,
            "notes": len(notes), "channels": sorted({c + 1 for c, _p, _s in notes}),
            "channel_notes": dict(Counter(c + 1 for c, _p, _s in notes)), "longest": longest, "flags": flags,
            "kb_share": s.length / sample_bytes, "play_share": heard / song_secs if song_secs else 0.0,
            "range": (note_name(lo), note_name(hi), AMIGA_CLOCK / lo) if lo is not None and hi is not None else None,
            "sounds": _bank_sounds(offsets.get(i, []), s.length) if i in banked else [],
        })
    used_cols = sorted({c for pos in mod.order for row in pats[pos] for c, cell in enumerate(row) if any(cell)})
    notes_out.append(f"{mod.tag} ({chans} channels), columns with anything in them: {[c + 1 for c in used_cols]}")
    total = sum(s.length for s in mod.samples)
    dead = sum(r["bytes"] for r in rows_out if "unused" in r["flags"])
    notes_out.append(f"samples: {total / 1024:.0f} KB in {sum(1 for s in mod.samples if s.length)} slots; "
                     f"{dead / 1024:.0f} KB unused; speed {speed}, {bpm} BPM ({row_secs * 1000:.0f} ms a row); "
                     f"{song_secs:.1f} s to the loop")
    return rows_out, notes_out


def _bank_sounds(hits: list[tuple[int, int, float]], size: int) -> list[dict]:
    """Per sound of a bank (its 9xx offset): bytes to the next sound, notes, seconds heard."""
    starts = sorted({off for off, _p, _s in hits})
    out = []
    for k, off in enumerate(starts):
        end = starts[k + 1] * 256 if k + 1 < len(starts) else size
        mine = [(p, s) for o, p, s in hits if o == off]
        out.append({"offset": off, "bytes": end - off * 256, "notes": len(mine), "secs": sum(s for _p, s in mine)})
    return out
