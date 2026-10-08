"""Symbolic pitch audit of a converted MOD against its VGM/VGZ recording: no audio rendered.

The chip side is each channel's pitch timeline (core.vgm.pitch_segments: every YM2612 $A4/$A0
write and key on/off, every SN76489 tone/volume write), so pitch changes under smpsNoAttack are
seen as well as key-ons.  The MOD side is the pitch each pattern note sounds at, from the config:
a note at MOD index n on an instrument whose `root` sounds pitch s (core.plan.sounding_pitches)
sounds at

    f = 440 * 2^((s - 57) / 12) * period[root] / period[n]      (* 2^(finetune / 96), * its detune)

Every chip segment longer than min_ms is compared with the MOD note sounding at its midpoint.
The authority on "is every note right": an audio window's pitch is unreliable on legato runs and
1-tick grace notes (GHZ FM3-FM5), which this is immune to.

    prepare_audit       the instruments as the converter prepared them (core.plan.prepare_instruments)
    mod_pitch_timeline  per MOD channel, (seconds, Hz, instrument) at each note and E1x / E2x
    note_start_offset   how far the MOD lags the recording, from note starts
    audit_pitches       per channel ok / wrong / missing, and per instrument the verdict
"""

from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from ..audio import pitch_name, semitone_to_hz
from ..config import SAMPLE_FINETUNE, SAMPLE_SLOT, ConversionConfig, find_settings, load_settings
from ..mod import PERIOD_TABLE, ModImage, edx_delay, timed_pass
from ..plan import prepare_instruments, sounding_pitches
from ..vgm import Segment


def audit_settings(settings_path: str | Path | None, config_path: str | Path):
    """(SynthesisSettings, PsgSynthesisSettings) the MOD was converted with: `settings_path`, else the
    settings.yaml beside the config, else configs/settings.yaml."""
    return load_settings(str(settings_path) if settings_path else find_settings(str(config_path)))


def prepare_audit(cfg: ConversionConfig, settings_path: str | Path | None, config_path: str | Path):
    """What the converter decides before it renders, on `cfg` (core.plan.prepare_instruments: every
    entry's synth_root / synth_shift, the detune variants the settings ask for).  Returns the song."""
    song = cfg.read_song()
    synth, _psg = audit_settings(settings_path, config_path)
    prepare_instruments(song, cfg, synth)
    return song


def mod_pitch_timeline(mod: ModImage, cfg: ConversionConfig, song) -> tuple[dict[int, list[tuple]], float]:
    """Per MOD channel list of (time, Hz, instrument); follows Bxx/Dxx and stops at the loop."""
    inst = sounding_pitches(song, cfg)
    finetune = {e[SAMPLE_SLOT]: (e[SAMPLE_FINETUNE] if len(e) > SAMPLE_FINETUNE else 0) for e in (cfg.sample_list or [])}
    known = set(PERIOD_TABLE)
    out: dict[int, list[tuple]] = defaultdict(list)
    sounding: dict[int, tuple[int, int]] = {}         # channel -> (period, instrument) of its note

    def pitch(period: int, ins: int) -> float:
        root, synth, cents = inst[ins]
        return (semitone_to_hz(synth) * PERIOD_TABLE[root] / period
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


def note_start_offset(chip: dict[str, list[Segment]], mod: dict[int, list[tuple]], chan_map: dict[str, int],
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
    ks = range(int(-0.5 / step), int(max_lag / step) + 1)
    lags = [k * step for k in ks]
    scorer = _StartScorer(starts, {c: sorted(notes) for c, notes in mod.items()}, lags, step, drift)

    # A repeating figure makes note starts alone ambiguous by its period (Drowning alternates
    # two notes every 200 ms), so a start only counts when the MOD note there has its pitch.
    # If that finds nothing at all (every instrument wrong), starts alone decide.
    for use_pitch in (True, False):
        best, best_lag = -1, 0.0
        for k, lag in zip(ks, lags, strict=True):
            hits = scorer.score(lag, use_pitch)
            if hits > best or (hits == best and abs(k) < abs(best_lag / step)):
                best, best_lag = hits, lag
        if best > 0:
            return best_lag
    return 0.0


def _same_pitch(f: float, hz: float) -> bool:
    """Within 50 cents, any octave: a sample synthesised in the wrong octave must not hide the
    alignment (the audit reports that separately)."""
    c = 1200 * math.log2(hz / f) % 1200
    return c <= 50 or c >= 1150


class _StartScorer:
    """note_start_offset's score of one lag, per channel in numpy: each chip note start against the
    MOD notes either side of start + lag - 3 points within `step`, 2 within two, 1 within the
    drift allowance.  Whether a candidate has the start's pitch is worked out once, with
    math.log2, for every MOD note any lag can reach."""

    def __init__(self, starts: list[tuple[int, float, float]], by_chan: dict[int, list[tuple]], lags: list[float],
                 step: float, drift: float) -> None:
        self._step = step
        self._chans = []
        for c, notes in by_chan.items():
            mine = [(t, f) for sc, t, f in starts if sc == c]
            if not mine or not notes:
                continue
            ts = np.array([n[0] for n in notes])
            st = np.array([t for t, _ in mine])
            near = 2 * step + drift * st
            lo = np.searchsorted(ts, st + lags[0], "left") - 1          # first candidate any lag reaches
            hi = np.searchsorted(ts, st + lags[-1], "left")             # last
            pitched = np.zeros((len(mine), int((hi - lo).max()) + 1), dtype=bool)
            for k, (_t, f) in enumerate(mine):
                for j in range(max(int(lo[k]), 0), min(int(hi[k]), len(ts) - 1) + 1):
                    pitched[k, j - lo[k]] = _same_pitch(f, notes[j][1])
            self._chans.append((ts, st, near, lo, pitched))

    def score(self, lag: float, use_pitch: bool) -> int:
        step, hits = self._step, 0
        for ts, st, near, lo, pitched in self._chans:
            want = st + lag
            i = np.searchsorted(ts, want, "left")
            rows = np.arange(len(st))
            dev = np.full(len(st), np.inf)
            for j in (i - 1, i):
                ok = (j >= 0) & (j < len(ts))
                if use_pitch:
                    ok &= pitched[rows, np.clip(j - lo, 0, pitched.shape[1] - 1)]
                dev = np.where(ok, np.minimum(dev, np.abs(ts[np.clip(j, 0, len(ts) - 1)] - want)), dev)
            hits += int(np.where(dev <= step, 3, np.where(dev <= 2 * step, 2, np.where(dev <= near, 1, 0))).sum())
        return hits


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


def audit_pitches(chip: dict[str, list[Segment]], vgm_end: float, mod: dict[int, list[tuple]], mod_end: float,
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
