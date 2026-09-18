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

Both sides are in real Hz: FM frequencies come from tools/vgm_analyze._fnum_to_hz and a
``synth_root`` name is the pitch the synthesiser actually renders (freq_to_fnum_block).

Usage::

    python tools/vgm_pitch_audit.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz"
    python tools/vgm_pitch_audit.py cfg.yaml ref.vgz --mod output/x.mod --min-ms 40 --list
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import itertools
import json
import math
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig
from core.tables import PERIOD_TABLE
from tools.vgm_analyze import _fnum_to_hz

_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
_MOD_FORMAT_CHANNELS = {"M.K.": 4, "M!K!": 4, "6CHN": 6, "8CHN": 8, "10CH": 10, "12CH": 12, "16CH": 16}
_VGM_RATE = 44100
# VGM command -> total length in bytes, for the commands that carry no timing or pitch.
_SKIP = {0x4F: 2, 0xE0: 5, 0x90: 5, 0x91: 5, 0x92: 6, 0x93: 11, 0x94: 2, 0x95: 5}

Segment = tuple[float, float | None]          # (start s, Hz or None when silent)


def note_name(f: float) -> str:
    m = round(69 + 12 * math.log2(f / 440.0))
    return f"{_NOTE_NAMES[m % 12]}{m // 12 - 1}"


def chip_timeline(data: bytes) -> tuple[dict[str, list[Segment]], float]:
    """Per-channel list of (time, sounding Hz | None) change points, and the recording length."""
    ver = struct.unpack_from("<I", data, 0x08)[0]
    rel = struct.unpack_from("<I", data, 0x34)[0] if ver >= 0x150 else 0
    pos = 0x34 + rel if rel else 0x40
    fm_clock = (struct.unpack_from("<I", data, 0x2C)[0] & 0x3FFF_FFFF) or 7_670_454
    psg_clock = (struct.unpack_from("<I", data, 0x0C)[0] & 0x3FFF_FFFF) or 3_579_545

    t = 0
    hi, freq, keyon = [0] * 6, [0.0] * 6, [False] * 6
    psg_n, psg_vol, latch = [0, 0, 0], [15] * 4, (0, 0)
    out: dict[str, list[Segment]] = defaultdict(list)

    def fm_mark(ch: int) -> None:
        out[f"FM{ch + 1}"].append((t / _VGM_RATE, freq[ch] if keyon[ch] and freq[ch] > 0 else None))

    def psg_mark(ch: int) -> None:
        f = psg_clock / (32.0 * psg_n[ch]) if psg_n[ch] > 0 and psg_vol[ch] < 15 else None
        out[f"PSG{ch + 1}"].append((t / _VGM_RATE, f))

    while pos < len(data):
        c = data[pos]
        if c in (0x52, 0x53):
            reg, val = data[pos + 1], data[pos + 2]
            base = 0 if c == 0x52 else 3
            if c == 0x52 and reg == 0x28:
                ch = val & 7
                ch = ch if ch < 3 else ch - 1
                if ch < 6:
                    keyon[ch] = bool(val & 0xF0)
                    fm_mark(ch)
            elif 0xA4 <= reg <= 0xA6:
                hi[base + reg - 0xA4] = val
            elif 0xA0 <= reg <= 0xA2:                 # low byte latches the pair
                ch = base + reg - 0xA0
                fnum = ((hi[ch] & 7) << 8) | val
                freq[ch] = _fnum_to_hz(fnum, (hi[ch] >> 3) & 7, fm_clock) if fnum else 0.0
                fm_mark(ch)
            pos += 3
        elif c == 0x50:
            b = data[pos + 1]
            if b & 0x80:
                latch = ((b >> 5) & 3, (b >> 4) & 1)
            ch, is_vol = latch
            if is_vol:
                psg_vol[ch] = b & 15
            elif ch < 3:
                psg_n[ch] = ((psg_n[ch] & 0x3F0) | (b & 15)) if b & 0x80 else ((psg_n[ch] & 0x00F) | ((b & 0x3F) << 4))
            if ch < 3:
                psg_mark(ch)
            pos += 2
        elif c == 0x61:
            t += struct.unpack_from("<H", data, pos + 1)[0]
            pos += 3
        elif c in (0x62, 0x63):
            t += 735 if c == 0x62 else 882
            pos += 1
        elif 0x70 <= c <= 0x8F:                       # 7n: wait n+1;  8n: DAC write + wait n
            t += (c & 15) + (1 if c < 0x80 else 0)
            pos += 1
        elif c == 0x66:
            break
        elif c == 0x67:
            pos += 7 + struct.unpack_from("<I", data, pos + 3)[0]
        elif c in _SKIP:
            pos += _SKIP[c]
        else:
            raise SystemExit(f"ERROR: unsupported VGM command {c:#04x} at offset {pos:#x}")
    return out, t / _VGM_RATE


def instrument_pitches(cfg: ConversionConfig) -> dict[int, tuple[int, int]]:
    """MOD instrument -> (root MOD index, synthesis semitone) from every pitched map entry."""
    inst: dict[int, tuple[int, int]] = {}

    def add(e, default_low: bool) -> None:
        if e.mod_instrument in inst or e.root is None:
            return
        if e.synth_root is not None:
            s = e.synth_root
        elif default_low and e.low is not None:       # FM: sample_generator falls back to `low`
            s = e.low
        else:                                         # PSG tone: falls back to `root`
            s = e.root.value + 12
        inst[e.mod_instrument] = (e.root.value, s)

    for lst in cfg.voice_map.values():
        for e in lst:
            add(e, True)
    for per_voice in cfg.channel_instrument_map.values():
        for lst in per_voice.values():
            for e in lst:
                add(e, True)
    for lst in cfg.psg_voice_map.values():
        for e in lst:
            if e.type == "tone":
                add(e, False)
    return inst


def mod_timeline(mod: bytes, cfg: ConversionConfig) -> tuple[dict[int, list[tuple]], float]:
    """Per MOD channel list of (time, Hz, instrument); follows Bxx/Dxx and stops at the loop."""
    inst = instrument_pitches(cfg)
    finetune = {e[0]: (e[3] if len(e) > 3 else 0) for e in (cfg.sample_list or [])}
    nch = _MOD_FORMAT_CHANNELS.get(mod[1080:1084].decode("ascii", "replace"), 4)
    order = list(mod[952:952 + mod[950]])
    known = set(PERIOD_TABLE)
    speed, bpm = cfg.target_speed, 125
    out: dict[int, list[tuple]] = defaultdict(list)
    now, posi, row = 0.0, 0, 0
    seen: set[tuple[int, int]] = set()
    while posi < len(order) and (posi, row) not in seen:
        seen.add((posi, row))
        base = 1084 + (order[posi] * 64 + row) * nch * 4
        jump = brk = None
        for c in range(nch):
            b = mod[base + c * 4:base + c * 4 + 4]
            period, ins = ((b[0] & 15) << 8) | b[1], (b[0] & 0xF0) | (b[2] >> 4)
            eff, par = b[2] & 15, b[3]
            if eff == 0xF:
                if par >= 0x20:
                    bpm = par
                elif par:
                    speed = par
            elif eff == 0xB:
                jump = par
            elif eff == 0xD:
                brk = (par >> 4) * 10 + (par & 15)
            if period in known and ins in inst:
                root, synth = inst[ins]
                f = (440.0 * 2 ** ((synth - 57) / 12) * PERIOD_TABLE[root] / period
                     * 2 ** (finetune.get(ins, 0) / 96))
                out[c].append((now, f, ins))
        now += speed * 2.5 / bpm
        if jump is not None:
            if jump <= posi:                          # the song loop
                break
            posi, row = jump, brk or 0
        elif brk is not None:
            posi, row = posi + 1, brk
        else:
            row += 1
            if row == 64:
                posi, row = posi + 1, 0
    return out, now


def auto_offset(chip: dict[str, list[Segment]], mod: dict[int, list[tuple]], chan_map: dict[str, int],
                max_lag: float = 3.0, step: float = 0.005) -> float:
    """Seconds the MOD lags the recording: the lag at which most chip note starts meet a MOD note.

    Recordings rarely start on the song's first tick (the Title Screen rip starts at its first DAC
    hit, 250 ms in), and without the lag every comparison reads the neighbouring note.
    """
    starts: list[tuple[int, float]] = []
    for src, evs in chip.items():
        if src in chan_map:
            prev = None
            for t, f in evs:
                if f is not None and (prev is None or abs(1200 * math.log2(f / prev)) > 50):
                    starts.append((chan_map[src], t))
                prev = f
    grid = {c: {round(n[0] / step) for n in notes} for c, notes in mod.items()}
    best, best_lag = -1, 0.0
    for k in range(int(-0.5 / step), int(max_lag / step) + 1):
        hits = 0
        for c, t in starts:
            g = grid.get(c)
            if g:
                q = round(t / step) + k
                hits += (q in g) or (q - 1 in g) or (q + 1 in g) or (q - 2 in g) or (q + 2 in g)
        if hits > best or (hits == best and abs(k) < abs(best_lag / step)):
            best, best_lag = hits, k * step
    return best_lag


def instrument_verdicts(by_inst: dict[int, Counter]) -> list[dict]:
    """Per instrument: notes, ok, and `semitones` != 0 when at least 80 % of its notes are out by that
    same interval (and fewer than 20 % are right) — i.e. the sample is synthesised at the wrong pitch
    and its synth_root is off by exactly that much.  A note-level problem never looks like this."""
    out = []
    for ins in sorted(by_inst):
        errs = by_inst[ins]
        notes, ok = sum(errs.values()), errs[0]
        semitones, uniform_notes = 0, 0
        wrong = [(c, k) for c, k in errs.most_common() if c != 0]
        if wrong:
            c, k = wrong[0]
            if k >= 0.8 * notes and ok < 0.2 * notes:
                semitones, uniform_notes = c // 100, k
        out.append({"instrument": ins, "notes": notes, "ok": ok, "semitones": semitones,
                    "uniform_notes": uniform_notes, "other": [] if semitones else wrong})
    return out


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
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    cfg = ConversionConfig.from_yaml(args.config)
    mod_path = Path(args.mod or cfg.output_file)
    raw = Path(args.vgz).read_bytes()
    chip, vgm_end = chip_timeline(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    mod, mod_end = mod_timeline(mod_path.read_bytes(), cfg)
    chan_map = {c.source: c.mod_channel for c in cfg.channels}
    if args.offset is None:
        args.offset = auto_offset(chip, mod, chan_map)
        how = "auto"
    else:
        how = "given"
    print(f"VGM {vgm_end:.1f} s   MOD one pass {mod_end:.1f} s   MOD lags by {args.offset * 1000:+.0f} ms ({how})   ({mod_path})")
    print(f"chip segments >= {args.min_ms:g} ms; wrong = more than {args.tolerance:g} cents from the chip")
    print()

    total_bad = 0
    by_inst: dict[int, Counter] = defaultdict(Counter)      # instrument -> {cents error rounded to 100: notes}
    for src in sorted(chip):
        if src not in chan_map or not mod.get(chan_map[src]):
            continue
        notes = mod[chan_map[src]]
        evs = [*chip[src], (vgm_end, None)]
        stats: Counter = Counter()
        wrong: Counter = Counter()
        listing: list[str] = []
        for (t0, f), (t1, _) in itertools.pairwise(evs):
            if f is None or t0 > mod_end - args.offset or t1 - t0 < 1e-4:
                continue
            if (t1 - t0) * 1000 < args.min_ms:
                stats["short"] += 1
                continue
            mid = (t0 + t1) / 2 + args.offset
            hit = None
            for n in notes:
                if n[0] > mid + 1e-6:
                    break
                hit = n
            if hit is None:
                stats["missing"] += 1
                listing.append(f"      {t0:7.2f} s  chip {note_name(f):<4}  no MOD note yet")
                continue
            cents = 1200 * math.log2(hit[1] / f)
            by_inst[hit[2]][0 if abs(cents) <= args.tolerance else round(cents / 100) * 100] += 1
            if abs(cents) <= args.tolerance:
                stats["ok"] += 1
            else:
                stats["wrong"] += 1
                wrong[(note_name(f), note_name(hit[1]), round(cents / 100) * 100, hit[2])] += 1
                listing.append(f"      {t0:7.2f} s  chip {note_name(f):<4}  MOD {note_name(hit[1]):<4} "
                               f"{cents:+6.0f} c  inst {hit[2]} (placed {hit[0]:.2f} s)")
        total_bad += stats["wrong"] + stats["missing"]
        print(f"{src:<5} ok {stats['ok']:>4}   wrong {stats['wrong']:>3}   missing {stats['missing']:>3}"
              f"   (+{stats['short']} shorter than {args.min_ms:g} ms)")
        for (a, b, c100, ins), k in wrong.most_common(8):
            print(f"        chip {a:<4} MOD {b:<4} ({c100:+5d} c)  inst {ins:<3} x{k}")
        if args.list:
            print("\n".join(listing))

    # An instrument whose notes are all out by the same interval is synthesised at the wrong pitch:
    # its synth_root is off by that interval.  Anything else is a note problem.
    verdicts = instrument_verdicts(by_inst)
    if any(v["semitones"] or v["other"] for v in verdicts):
        print()
        print("Per instrument (the same error on nearly every note = synth_root off by that interval)")
        for v in verdicts:
            if v["semitones"]:
                n = abs(v["semitones"])
                size = f"{n // 12} octave{'s' if n // 12 > 1 else ''}" if n % 12 == 0 else f"{n} semitone{'s' if n > 1 else ''}"
                verdict = (f"synth_root is {size} too {'high' if v['semitones'] > 0 else 'low'} "
                           f"({v['uniform_notes']} of {v['notes']} notes)")
            elif v["other"]:
                verdict = "mixed: " + ", ".join(f"{c:+d} c x{k}" for c, k in v["other"][:4])
            else:
                continue
            print(f"  inst {v['instrument']:>2}: ok {v['ok']:>4}  wrong {v['notes'] - v['ok']:>4}   {verdict}")
    if args.json:
        Path(args.json).write_text(json.dumps({"offset_s": args.offset, "instruments": verdicts}, indent=2) + "\n",
                                   encoding="utf-8")
    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    main()
