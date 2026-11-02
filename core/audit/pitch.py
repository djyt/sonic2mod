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

    mod_pitch_timeline  per MOD channel, (seconds, Hz, instrument, attack) at each note, and at
                        each step of an E1x / E2x / 1xx / 2xx (attack False: only notes pair with
                        a chip note's start)
    note_start_offset   how far the MOD lags the recording, from note starts
    audit_pitches       per channel ok / wrong / missing, and per instrument the verdict; with
                        `trace`, each chip segment as it was judged (PairStep: vgm_pitch_audit --trace)

A slide (Time Trax's glides) moves the chip's pitch every frame, each segment shorter than min_ms:
none would be judged.  With `slide_tolerance`, a run of two or more short segments is a slide, each
segment judged against the MOD's pitch at its midpoint (its 1xx / 2xx steps: mod_pitch_timeline)
within that tolerance, counted apart (slide_ok, slide_off).  A lone short segment (a grace note)
stays unjudged; a vibrato's steps run as a slide too, against the note's centre (4xy is not followed).

    chip   C ------- C# D D# E ---------         segments: C (held), C# D D# (short: the slide), E
    MOD    C ------- 1xx steps ... E -----       C# D D# each against the MOD's pitch at its midpoint
"""

from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..audio import pitch_name, semitone_to_hz
from ..chips import freq_word_hz
from ..mod import FINETUNE_STEPS, PERIOD_TABLE, TICK_SECS_AT_1_BPM, ModImage, edx_delay, timed_pass
from ..plan import ConversionPlan, sounding_pitches
from ..smps import fm_render_word, update_hz
from ..vgm import Segment

_SLIDE_UP, _SLIDE_DOWN = 0x1, 0x2          # 1xx / 2xx
_ATTACK = 3                                 # a timeline entry's: a note-on, not a slide's step
_HELD = 0.025                               # s a pitch holds for a move from it to start a note (> a frame)
_INSTANT = 0.008                            # s: a segment shorter is one write of a burst the next replaces
                                            # (a burst lasts up to 7.5 ms; a frame is 16.7)


@dataclass(frozen=True)
class PairStep:
    """One chip segment as audit_pitches judged it."""

    channel: str
    start: float                # s, the recording's
    end: float
    hz: float
    note_start: bool            # it starts a chip note (else a continuation: vibrato, legato)
    deviation: float            # s, MOD minus chip, as the segment was judged
    paired: float | None        # the MOD note start it paired with; None: looked up at its midpoint
    mod: tuple | None           # (s, Hz, instrument, attack) of the MOD note judged against
    verdict: str                # ok, wrong, short, missing; with slides judged: slide ok, slide off


def mod_pitch_timeline(mod: ModImage, conv: ConversionPlan, song) -> tuple[dict[int, list[tuple]], float]:
    """Per MOD channel list of (time, Hz, instrument, attack), as `conv` (the conversion's rendering
    pitches and detune variants) made the samples; follows Bxx/Dxx and stops at the loop.  A sample
    that carries a sweep (core.plan.baked) moves by its envelope each frame, to the next note-on."""
    cfg = conv.config
    inst = sounding_pitches(song, conv)
    swept = _swept(song, conv, inst)
    finetune = {slot: row.finetune for slot, row in conv.samples.rows().items()}
    known = set(PERIOD_TABLE)
    out: dict[int, list[tuple]] = defaultdict(list)
    sounding: dict[int, tuple[int, int]] = {}         # channel -> (period, instrument) of its note
    sweeps: dict[int, list[tuple[float, list[tuple]]]] = defaultdict(list)    # channel -> (note-on, its sweep)

    def pitch(period: int, ins: int) -> float:
        root, synth, cents = inst[ins]
        return (semitone_to_hz(synth) * PERIOD_TABLE[root] / period
                * 2 ** (finetune.get(ins, 0) / FINETUNE_STEPS + cents / 1200))

    rows, end = timed_pass(mod, cfg.target_speed)
    for r in rows:
        for c, (period, ins, eff, par) in enumerate(r.cells):
            if period in known and ins in inst:
                sounding[c] = (period, ins)
                start = r.start + edx_delay(eff, par, r.bpm)
                out[c].append((start, pitch(period, ins), ins, True))
                if ins in swept:
                    sweeps[c].append((start, [(start + t, pitch(period, ins) * ratio, ins, False) for t, ratio in swept[ins]]))
            if eff in (_SLIDE_UP, _SLIDE_DOWN) and par and c in sounding:
                # 1xx / 2xx: the period moves by xx on each tick after the row's first (a sweep)
                p, ins_s = sounding[c]
                for tick in range(1, cfg.target_speed):
                    p += -par if eff == _SLIDE_UP else par
                    out[c].append((r.start + tick * TICK_SECS_AT_1_BPM / r.bpm, pitch(p, ins_s), ins_s, False))
                sounding[c] = (p, ins_s)
            elif eff == 0xE and par >> 4 in (1, 2) and c in sounding:
                # E1x / E2x: the sounding note's period moved (a tie retuned to a new detune)
                p, ins_s = sounding[c]
                p = p - (par & 15) if par >> 4 == 1 else p + (par & 15)
                sounding[c] = (p, ins_s)
                out[c].append((r.start, pitch(p, ins_s), ins_s, False))
    return _with_sweeps(out, sweeps), end


def _swept(song, conv: ConversionPlan, inst: dict) -> dict[int, list[tuple[float, float]]]:
    """{instrument: [(seconds after its note-on, its pitch ratio)]} of each sample rendered with a
    sweep's envelope: a word offset each frame from the word its notes' pitch class plays."""
    if conv.baked is None or conv.detune is None:
        return {}
    frame = 1 / update_hz(song.rules, conv.config.region)
    out = {}
    for ins, (_root, semitone, _cents) in inst.items():
        envelope = conv.baked.pitch_envelopes().get(conv.detune.envelope_of(ins))
        if envelope is None:
            continue
        word = fm_render_word(semitone, song.rules.fm_frequencies)
        offsets = envelope.offsets(len(envelope.steps))
        out[ins] = [(f * frame, freq_word_hz(word + o) / freq_word_hz(word)) for f, o in enumerate(offsets) if f]
    return out


def _with_sweeps(out: dict[int, list[tuple]], sweeps: dict[int, list[tuple[float, list[tuple]]]]) -> dict[int, list[tuple]]:
    """Each channel's pitches with its sweeps' in, each cut at the channel's next note-on."""
    for c, swept in sweeps.items():
        attacks = sorted(p[0] for p in out[c] if p[3])
        for start, points in swept:
            later = [t for t in attacks if t > start]
            out[c] += [p for p in points if not later or p[0] < later[0]]
        out[c].sort(key=lambda p: p[0])
    return dict(out)


_OPENING_SECS = 30.0            # the chip note starts that decide where the song starts


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
    20 ms range of lags.  The opening alone decides (_OPENING_SECS of chip starts): over a long
    song the allowance lets a lag partway along the drift outscore the start (OutRun's Splash
    Wave, 339 s at 0.14 % off: -260 ms, every note of its arpeggio judged against the next).
    """
    starts: list[tuple[int, float, float]] = []
    for src, evs in chip.items():
        if src in chan_map:
            prev = None
            for t, f in _settled(evs):
                if f is not None and (prev is None or abs(1200 * math.log2(f / prev)) > 50):
                    starts.append((chan_map[src], t, f))
                prev = f
    if starts:
        opening = min(t for _, t, _ in starts) + _OPENING_SECS
        starts = [s for s in starts if s[1] < opening]
    ks = range(int(-max_lag / step), int(max_lag / step) + 1)       # either way: a rip may start before its song (Time Trax's: 0.8 s)
    lags = [k * step for k in ks]
    scorer = _StartScorer(starts, {c: sorted(n for n in notes if n[_ATTACK]) for c, notes in mod.items()},
                          lags, step, drift)

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


def _judge(st: dict, by_inst: dict[int, Counter], t0: float, t1: float, f: float, hit: tuple | None,
           min_ms: float, tolerance: float) -> str:
    """A chip segment against the MOD note `hit`, counted in `st` and `by_inst`: its verdict."""
    if (t1 - t0) * 1000 < min_ms:
        st["short"] += 1
        return "short"
    if hit is None:
        st["missing"] += 1
        st["missing_notes"].append({"t_s": t0, "chip": pitch_name(f)})
        return "missing"
    cents = 1200 * math.log2(hit[1] / f)
    by_inst[hit[2]][0 if abs(cents) <= tolerance else round(cents / 100) * 100] += 1
    if abs(cents) <= tolerance:
        st["ok"] += 1
        return "ok"
    st["wrong"] += 1
    st["wrong_notes"].append({"t_s": t0, "chip": pitch_name(f), "mod": pitch_name(hit[1]), "cents": cents,
                              "instrument": hit[2], "placed_s": hit[0]})
    return "wrong"


def _judge_slide(st: dict, t0: float, f: float, hit: tuple | None, tolerance: float) -> str:
    """A slide's chip segment against the MOD's pitch at its midpoint (`hit`), counted in `st`."""
    cents = 1200 * math.log2(hit[1] / f) if hit is not None else math.inf
    if abs(cents) <= tolerance:
        st["slide_ok"] += 1
        return "slide ok"
    st["slide_off"] += 1
    st["slide_notes"].append({"t_s": t0, "chip": pitch_name(f), "mod": pitch_name(hit[1]) if hit else "-",
                              "cents": cents})
    return "slide off"


def _slide_segments(evs: list[Segment], min_ms: float) -> list[bool]:
    """Which of `evs`' segments (each to the next's start) are a slide's: sounding and shorter than
    `min_ms`, beside another such."""
    short = [f is not None and (t1 - t0) * 1000 < min_ms for (t0, f), (t1, _) in itertools.pairwise(evs)]
    return [s and ((i > 0 and short[i - 1]) or (i + 1 < len(short) and short[i + 1])) for i, s in enumerate(short)]


def _settled(segments: list[Segment]) -> list[Segment]:
    """`segments` without a pitch the next replaces within the driver's burst: a PSG note's
    attenuation written before its tone holds the last note's pitch for a moment (Streets of Rage
    2's), and took the new note's start with it.  A silence stays: a key-off and key-on in one
    burst re-strike a note at its pitch."""
    return [seg for seg, after in zip(segments, [*segments[1:], None], strict=True)
            if after is None or seg[1] is None or after[0] - seg[0] >= _INSTANT]


def _chip_starts(segments: list[Segment]) -> list[bool]:
    """Which segments start a note: the channel was silent, or the pitch moved by more than 50
    cents from one that held.  Anything else is a continuation: a vibrato step, a detune scoop,
    a sweep's steps a frame apart (Streets of Rage $90 FM4: 8 FNUM units a frame)."""
    out, prev, prev_t = [], None, None
    for t, f in segments:
        held = prev_t is not None and t - prev_t >= _HELD
        out.append(f is not None and (prev is None or (held and abs(1200 * math.log2(f / prev)) > 50)))
        prev, prev_t = f, t
    return out


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
          chan_map: dict[str, int], offset: float, min_ms: float = 60.0, tolerance: float = 35.0,
          trace: Callable[[PairStep], None] | None = None, slide_tolerance: float | None = None) -> dict:
    """Compare every chip segment of at least `min_ms` with the MOD note sounding at its midpoint.

    Returns {"channels": {source: {ok, wrong, missing, short, wrong_notes, missing_notes}},
             "instruments": instrument_verdicts(...), "bad": wrong + missing over all channels}.
    With `slide_tolerance` (cents), slides are judged too: each channel's slide_ok, slide_off and
    slide_notes, and slide_off counted in "bad".

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
        timeline = mod[chan_map[src]]
        notes = [n for n in timeline if n[_ATTACK]]         # what a chip note's start pairs with
        segments = _settled(chip[src])
        evs = [*segments, (vgm_end, None)]
        st: dict = {"ok": 0, "wrong": 0, "missing": 0, "short": 0, "wrong_notes": [], "missing_notes": []}
        if slide_tolerance is not None:
            st |= {"slide_ok": 0, "slide_off": 0, "slide_notes": []}
        slides = _slide_segments(evs, min_ms) if slide_tolerance is not None else [False] * len(segments)
        run, j = offset, 0          # running MOD-minus-chip deviation (s); next unpaired MOD note
        starts = _chip_starts(segments)
        starts_t = [t for (t, _), s in zip(segments, starts, strict=True) if s]   # for looking past a tempo step
        si = -1
        for i, ((t0, f), (t1, _)) in enumerate(itertools.pairwise(evs)):
            is_start = starts[i]
            # A segment starting as the MOD's single pass ends is the recording going round its
            # loop; the last MOD note must not be judged against it.
            if f is None or t0 + run > mod_end - 0.03:
                continue
            # Pair a note start with the next MOD note near it (MOD-only notes in between are
            # skipped), and let the deviation follow.
            paired = None
            if is_start:
                si += 1
                while j < len(notes) and notes[j][0] - t0 < run - 0.04:
                    j += 1
                extra = (si + 1 < len(starts_t) and j < len(notes)
                         and abs(notes[j][0] - starts_t[si + 1] - run) <= 0.04)
                if j < len(notes) and abs(notes[j][0] - t0 - run) > 0.04 and not extra:
                    # A step in the deviation that the next two starts confirm is a tempo
                    # change (the MOD falls up to two frames behind at each smpsSetTempoMod);
                    # not where the next start meets this MOD note as things stand: this start
                    # is the rip's alone (Streets of Rage 2's PSG2 unmuted on a stale tone)
                    d = notes[j][0] - t0
                    if abs(d - run) <= 0.12 and all(
                            si + n < len(starts_t) and j + n < len(notes)
                            and abs(notes[j + n][0] - starts_t[si + n] - d) <= 0.04 for n in (1, 2)):
                        run = d
            if is_start and j < len(notes) and abs(notes[j][0] - t0 - run) <= 0.04:
                paired = notes[j]
                run = 0.5 * run + 0.5 * (notes[j][0] - t0)
                j += 1
            hit = paired
            if hit is None:
                mid = (t0 + t1) / 2 + run
                for n in timeline:
                    if n[0] > mid + 1e-6:
                        break
                    hit = n
            if slides[i]:
                assert slide_tolerance is not None
                verdict = _judge_slide(st, t0, f, hit, slide_tolerance)
            else:
                verdict = _judge(st, by_inst, t0, t1, f, hit, min_ms, tolerance)
            if trace is not None:
                trace(PairStep(src, t0, t1, f, is_start, run, paired[0] if paired else None, hit, verdict))
        channels[src] = st
    return {"channels": channels, "instruments": instrument_verdicts(by_inst),
            "bad": sum(c["wrong"] + c["missing"] + c.get("slide_off", 0) for c in channels.values())}
