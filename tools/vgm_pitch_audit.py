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
import bisect
import contextlib
import itertools
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio import pitch_name
from core.config import ConversionConfig
from core.mod import PERIOD_TABLE, ModImage, edx_delay, read_mod, timed_pass
from core.vgm import Segment, pitch_segments, read_vgm


def prepare_config(cfg: ConversionConfig, settings_path: str | Path | None, config_path: str | Path):
    """What the converter decides before it renders, on `cfg`: every entry's synth_root /
    synth_shift (from the song) and the detune variants (core.plan.detune) the settings ask for.
    Returns the parsed song."""
    from core.config import find_settings, load_settings
    from core.plan import detune_variants_wanted, plan_detune_variants, resolve_synth_roots
    song = cfg.read_song()
    resolve_synth_roots(song, cfg)
    synth, _psg = load_settings(str(settings_path) if settings_path else find_settings(str(config_path)))
    if detune_variants_wanted(synth):
        plan_detune_variants(song, cfg)
    return song


def instrument_pitches(cfg: ConversionConfig) -> dict[int, tuple[int, int, float]]:
    """MOD instrument -> (root MOD index, synthesis semitone, cents its sample's smpsAlterNote
    detune adds) from every pitched map entry and detune variant (core.plan.detune)."""
    from core.plan import detune_cents
    inst: dict[int, tuple[int, int, float]] = {}
    plan = cfg.detune_plan

    def add(e, default_low: bool) -> None:
        if e.mod_instrument in inst or e.root is None:
            return
        if e.synth_root is not None:
            s = e.synth_root - e.synth_shift          # the pitch `root` sounds: the sample's rate carries the rest
            rendered = e.synth_root
        elif default_low and e.low is not None:       # FM: sample_generator falls back to `low`
            s = rendered = e.low
        else:                                         # PSG tone: falls back to `root`
            s = rendered = e.root.value + 12
        own = plan.own.get(e.mod_instrument, 0) if plan is not None and default_low else 0
        inst[e.mod_instrument] = (e.root.value, s, detune_cents(rendered, own) if own else 0.0)
        if plan is None or not default_low:
            return
        for v in plan.variants.values():
            if v.base == e.mod_instrument:
                inst[v.inst] = (e.root.value, s, detune_cents(rendered, v.detune))

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


def mod_timeline(mod: ModImage, cfg: ConversionConfig) -> tuple[dict[int, list[tuple]], float]:
    """Per MOD channel list of (time, Hz, instrument); follows Bxx/Dxx and stops at the loop."""
    inst = instrument_pitches(cfg)
    finetune = {e[0]: (e[3] if len(e) > 3 else 0) for e in (cfg.sample_list or [])}
    known = set(PERIOD_TABLE)
    out: dict[int, list[tuple]] = defaultdict(list)
    sounding: dict[int, tuple[int, int]] = {}         # channel -> (period, instrument) of its note

    def pitch(period: int, ins: int) -> float:
        root, synth, cents = inst[ins]
        return (440.0 * 2 ** ((synth - 57) / 12) * PERIOD_TABLE[root] / period
                * 2 ** (finetune.get(ins, 0) / 96 + cents / 1200))

    rows, end = timed_pass(mod, cfg.target_speed)
    for r in rows:
        for c, (period, ins, eff, par) in enumerate(r.cells):
            if period in known and ins in inst:
                sounding[c] = (period, ins)
                out[c].append((r.start + edx_delay(eff, par, r.bpm), pitch(period, ins), ins))
            elif eff == 0xE and par >> 4 in (1, 2) and c in sounding:
                # E1x / E2x: the sounding note's period moved (a tie retuned to a new detune)
                p, ins_s = sounding[c]
                p = p - (par & 15) if par >> 4 == 1 else p + (par & 15)
                sounding[c] = (p, ins_s)
                out[c].append((r.start, pitch(p, ins_s), ins_s))
    return out, end


def auto_offset(chip: dict[str, list[Segment]], mod: dict[int, list[tuple]], chan_map: dict[str, int],
                max_lag: float = 3.0, step: float = 0.005, drift: float = 0.006) -> float:
    """Seconds the MOD lags the recording: the lag at which most chip note starts meet a MOD note.

    Recordings rarely start on the song's first tick (the Title Screen rip starts at its first DAC
    hit, 250 ms in), and without the lag every comparison reads the neighbouring note.

    The lag is where the song STARTS; a MOD drifts against the recording as it plays (an integer
    BPM, or tempo steps each rounded - Drowning is 31 ms apart by its end), so a note at time t is
    allowed to sit `drift` x t away from the lag and still count.  Without that allowance a
    drifting song pulls the lag towards a neighbouring note that fits the later notes better
    (Drowning: +170 ms, every 200 ms note judged against the next).  Closer still counts for
    more: notes delayed by EDx sit a few ms off the rest, and a plain count would tie over a
    20 ms range of lags.
    """
    starts: list[tuple[int, float, float]] = []
    for src, evs in chip.items():
        if src in chan_map:
            prev = None
            for t, f in evs:
                if f is not None and (prev is None or abs(1200 * math.log2(f / prev)) > 50):
                    starts.append((chan_map[src], t, f))
                prev = f
    by_chan = {c: sorted(notes) for c, notes in mod.items()}
    times = {c: [n[0] for n in notes] for c, notes in by_chan.items()}

    def same_pitch(f: float, hz: float) -> bool:
        # Within 50 cents, any octave: a sample synthesised in the wrong octave must not hide
        # the alignment (the audit reports that separately).
        c = 1200 * math.log2(hz / f) % 1200
        return c <= 50 or c >= 1150

    def score(lag: float, use_pitch: bool) -> int:
        hits = 0
        for c, t, f in starts:
            ts = times.get(c)
            if not ts:
                continue
            want = t + lag
            i = bisect.bisect_left(ts, want)
            cands = [j for j in (i - 1, i) if 0 <= j < len(ts)]
            if use_pitch:
                cands = [j for j in cands if same_pitch(f, by_chan[c][j][1])]
            if not cands:
                continue
            dev = min(abs(ts[j] - want) for j in cands)
            hits += 3 if dev <= step else 2 if dev <= 2 * step else 1 if dev <= 2 * step + drift * t else 0
        return hits

    # A repeating figure makes note starts alone ambiguous by its period (Drowning alternates
    # two notes every 200 ms), so a start only counts when the MOD note there has its pitch.
    # If that finds nothing at all (every instrument wrong), starts alone decide.
    for use_pitch in (True, False):
        best, best_lag = -1, 0.0
        for k in range(int(-0.5 / step), int(max_lag / step) + 1):
            hits = score(k * step, use_pitch)
            if hits > best or (hits == best and abs(k) < abs(best_lag / step)):
                best, best_lag = hits, k * step
        if best > 0:
            return best_lag
    return 0.0


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


def audit(chip: dict[str, list[Segment]], vgm_end: float, mod: dict[int, list[tuple]], mod_end: float,
          chan_map: dict[str, int], offset: float, min_ms: float = 60.0, tolerance: float = 35.0) -> dict:
    """Compare every chip segment of at least `min_ms` with the MOD note sounding at its midpoint.

    Returns {"channels": {source: {ok, wrong, missing, short, wrong_notes, missing_notes}},
             "instruments": instrument_verdicts(...), "bad": wrong + missing over all channels}.

    `offset` is where the song starts; from there each channel follows its own drift: every chip
    segment start that has a MOD note within 40 ms of the running deviation is paired with it
    (one to one, in order), the deviation is updated, and a segment with no start of its own in
    the MOD (a legato pitch change) is looked up at its midpoint with the deviation as it stood.
    With a fixed offset a 30 ms drift misreads every 50 ms note near the end of Drowning.
    """
    channels: dict[str, dict] = {}
    by_inst: dict[int, Counter] = defaultdict(Counter)      # instrument -> {cents error rounded to 100: notes}
    for src in sorted(chip):
        if src not in chan_map or not mod.get(chan_map[src]):
            continue
        notes = mod[chan_map[src]]
        evs = [*chip[src], (vgm_end, None)]
        st: dict = {"ok": 0, "wrong": 0, "missing": 0, "short": 0, "wrong_notes": [], "missing_notes": []}
        run, j = offset, 0          # running MOD-minus-chip deviation (s); next unpaired MOD note
        starts_t, pf = [], None     # chip note-start times, for looking ahead past a tempo step
        for t, f in chip[src]:
            if f is not None and (pf is None or abs(1200 * math.log2(f / pf)) > 50):
                starts_t.append(t)
            pf = f
        si = -1
        prev_f = None
        for (t0, f), (t1, _) in itertools.pairwise(evs):
            # A note start = the channel was silent or the pitch moved by more than 50 cents;
            # anything else (a vibrato step, a detune scoop) is a continuation.
            is_start = f is not None and (prev_f is None or abs(1200 * math.log2(f / prev_f)) > 50)
            prev_f = f
            # A segment starting as the MOD's single pass ends is the recording going round its
            # loop; the last MOD note must not be judged against it.
            if f is None or t0 + run > mod_end - 0.03 or t1 - t0 < 1e-4:
                continue
            # Pair a note start with the next MOD note near it (MOD-only notes in between are
            # skipped), and let the deviation follow.
            paired = None
            if is_start:
                si += 1
                while j < len(notes) and notes[j][0] - t0 < run - 0.04:
                    j += 1
                if j < len(notes) and abs(notes[j][0] - t0 - run) > 0.04:
                    # A step in the deviation that the next two starts confirm is a tempo
                    # change (the MOD falls up to two frames behind at each smpsSetTempoMod).
                    d = notes[j][0] - t0
                    if abs(d - run) <= 0.12 and all(
                            si + n < len(starts_t) and j + n < len(notes)
                            and abs(notes[j + n][0] - starts_t[si + n] - d) <= 0.04 for n in (1, 2)):
                        run = d
            if is_start and j < len(notes) and abs(notes[j][0] - t0 - run) <= 0.04:
                paired = notes[j]
                run = 0.5 * run + 0.5 * (notes[j][0] - t0)
                j += 1
            if (t1 - t0) * 1000 < min_ms:
                st["short"] += 1
                continue
            hit = paired
            if hit is None:
                mid = (t0 + t1) / 2 + run
                for n in notes:
                    if n[0] > mid + 1e-6:
                        break
                    hit = n
            if hit is None:
                st["missing"] += 1
                st["missing_notes"].append({"t_s": t0, "chip": pitch_name(f)})
                continue
            cents = 1200 * math.log2(hit[1] / f)
            by_inst[hit[2]][0 if abs(cents) <= tolerance else round(cents / 100) * 100] += 1
            if abs(cents) <= tolerance:
                st["ok"] += 1
            else:
                st["wrong"] += 1
                st["wrong_notes"].append({"t_s": t0, "chip": pitch_name(f), "mod": pitch_name(hit[1]), "cents": cents,
                                          "instrument": hit[2], "placed_s": hit[0]})
        channels[src] = st
    return {"channels": channels, "instruments": instrument_verdicts(by_inst),
            "bad": sum(c["wrong"] + c["missing"] for c in channels.values())}


def verdict_text(v: dict) -> str:
    """One instrument_verdicts() entry as words; '' when the instrument has nothing wrong."""
    if v["semitones"]:
        n = abs(v["semitones"])
        size = f"{n // 12} octave{'s' if n // 12 > 1 else ''}" if n % 12 == 0 else f"{n} semitone{'s' if n > 1 else ''}"
        return (f"synth_root is {size} too {'high' if v['semitones'] > 0 else 'low'} "
                f"({v['uniform_notes']} of {v['notes']} notes)")
    if v["other"]:
        return "mixed: " + ", ".join(f"{c:+d} c x{k}" for c, k in v["other"][:4])
    return ""


def print_audit(res: dict, min_ms: float, listing: bool = False, indent: str = "") -> None:
    """The per-channel counts, the commonest wrong intervals, and the per-instrument verdicts."""
    for src, st in res["channels"].items():
        print(f"{indent}{src:<5} ok {st['ok']:>4}   wrong {st['wrong']:>3}   missing {st['missing']:>3}"
              f"   (+{st['short']} shorter than {min_ms:g} ms)")
        kinds = Counter((w["chip"], w["mod"], round(w["cents"] / 100) * 100, w["instrument"]) for w in st["wrong_notes"])
        for (a, b, c100, ins), k in kinds.most_common(8):
            print(f"{indent}        chip {a:<4} MOD {b:<4} ({c100:+5d} c)  inst {ins:<3} x{k}")
        if listing:
            rows = [(w["t_s"], f"chip {w['chip']:<4}  MOD {w['mod']:<4} {w['cents']:+6.0f} c  inst {w['instrument']} "
                               f"(placed {w['placed_s']:.2f} s)") for w in st["wrong_notes"]]
            rows += [(m["t_s"], f"chip {m['chip']:<4}  no MOD note yet") for m in st["missing_notes"]]
            for t, text in sorted(rows):
                print(f"{indent}      {t:7.2f} s  {text}")

    # An instrument whose notes are all out by the same interval is synthesised at the wrong pitch:
    # its synth_root is off by that interval.  Anything else is a note problem.
    if any(verdict_text(v) for v in res["instruments"]):
        print()
        print(f"{indent}Per instrument (the same error on nearly every note = synth_root off by that interval)")
        for v in res["instruments"]:
            if verdict_text(v):
                print(f"{indent}  inst {v['instrument']:>2}: ok {v['ok']:>4}  wrong {v['notes'] - v['ok']:>4}   {verdict_text(v)}")


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
    ap.add_argument("--settings", metavar="PATH",
                    help="settings the MOD was converted with (default: settings.yaml beside the config, "
                         "else configs/settings.yaml): whether it has detune variants")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    cfg = ConversionConfig.from_yaml(args.config)
    prepare_config(cfg, args.settings, args.config)       # synth roots and detune variants, as the converter
    mod_path = Path(args.mod or cfg.output_file)
    chip, vgm_end = pitch_segments(read_vgm(args.vgz))
    mod, mod_end = mod_timeline(read_mod(mod_path), cfg)
    chan_map = {c.source: c.mod_channel for c in cfg.channels}
    if args.offset is None:
        args.offset = auto_offset(chip, mod, chan_map)
        how = "auto"
    else:
        how = "given"
    print(f"VGM {vgm_end:.1f} s   MOD one pass {mod_end:.1f} s   MOD lags by {args.offset * 1000:+.0f} ms ({how})   ({mod_path})")
    print(f"chip segments >= {args.min_ms:g} ms; wrong = more than {args.tolerance:g} cents from the chip")
    print()

    res = audit(chip, vgm_end, mod, mod_end, chan_map, args.offset, args.min_ms, args.tolerance)
    print_audit(res, args.min_ms, listing=args.list)
    if args.json:
        Path(args.json).write_text(json.dumps({"offset_s": args.offset, "instruments": res["instruments"]}, indent=2) + "\n",
                                   encoding="utf-8")
    sys.exit(1 if res["bad"] else 0)


if __name__ == "__main__":
    main()
