"""Sustain loops: cutting a rendered sample where its envelope has settled, and the release
rate a MOD volume slide stands in for afterwards.

A MOD sample does not loop unless its header says so, so every synthesised sample used to
hold the longest ring any of its notes plays (`sustain_duration: auto`): a lead that holds
one note for 3 s cost 50 KB at 16 kHz, a chord composite twice that.  Most YM2612 voices
reach a steady sustain level (every decaying operator at its sustain level, D2R 0), after
which the waveform repeats at the note's fundamental; a PSG tone with a holding envelope
is a square wave from its first frame.  From that point on a whole number of cycles can
loop, and the sample can be cut there: its length no longer depends on the notes.

`find_sustain_loop` looks for that point and that loop in a rendered sample:

  1. The RMS envelope over windows of two fundamental periods, in dB below the sample's
     own peak.  The last `span_secs` of the sustain (before key-off) is the reference;
     the envelope is *flat* from the first window after which every window stays within
     `flat_db` of the reference span's range.  A span, not a single level, so a chorus pair
     that beats (two detuned voices, an octave-detuned composite) is flat once the beating
     itself is steady; a decaying voice never settles and gets no loop.
  2. Loop candidates start at the flat point (a few periods later too) and run every even
     length (a MOD loop is measured in words) from `min_loop_secs` to `max_loop_secs`.
     Each is scored by the discontinuity the loop would introduce: the RMS difference
     between the two periods after the loop start and the two after its end, relative to
     the signal - what is heard when playback jumps back.  A whole number of fundamental
     cycles is rarely the answer: nearly every Sonic 1 voice detunes its operators (DT1),
     so its waveform never repeats exactly and the best length is where the operators'
     phases come closest to recurring (a beating pair: one beat period).  The lowest
     score wins (numpy scores every length at once; without it only the fundamental's
     grid is tried).
     A loop that would end past `MAX_END_FRACTION` of the rendered sustain is refused: the
     envelope was still settling there (a slowly decaying voice), and a loop at its last
     level would freeze a decay that the hardware carries on.  The generators render a
     `PROBE_SECS` sustain to search in, and fall back to the note's own length without one.
  3. `apply_loop` closes the loop: the last `cross_secs` of it are crossfaded into the
     samples just before its start, so the jump lands on a matching waveform whatever
     the residual mismatch, and the sample is cut at the loop's end.

A voice under the hardware LFO (`cycle`: its period) never repeats within one cycle: its pitch
and, with AMS, its level move on every one.  Its envelope is measured over whole cycles, the
loop spans a whole number of them (within a fundamental period, where the waveform closes), and
the timbre check is off (the LFO moves the timbre itself).

With `decay` (a voice's `loop_decay: slide`) the level need not have settled: the flat point is
where the envelope starts to fall in a straight line (in dB, within `flat_db` of the line the
reference span draws) and the timbre holds from there.  The samples from that point on are
turned up by the fall (`flatten`), so the loop plays at the flat point's level, and the fall is
reported (SustainLoop.decay_db, dB per sample) for the converter to write as volume slides into
every note that rings past it: a bass that falls 4 dB a second for 3 s costs a loop, not 3 s.

`release_rate_db_s` measures how fast the level falls after key-off, in dB per second,
from the same render: with the sample cut at the loop, the converter has to end each note
itself, and a volume slide at that rate (an exponential one, row by row) is what a MOD can
do in place of the chip's release phase.  A voice whose release is over within a row is
cut as before (C00).

Both work on the raw mono render (ints or floats), before 8-bit quantisation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .gain import gain_to_db

# --- defaults ---------------------------------------------------------------------------------

FLAT_DB = 1.0             # a window is flat when within this of the reference span's range (the
                          # settings' loop_drift_db default)
SPAN_SECS = 1.0           # the reference span: the last part of the sustain before key-off
MIN_LOOP_SECS = 0.03      # the shortest loop (a frozen chorus phase sounds static)
MAX_LOOP_SECS = 1.2       # the longest loop tried (a detuned pair beating at 1 Hz needs a whole beat)
LENGTH_PENALTY = 0.05     # score = discontinuity + this per second of loop: a shorter loop wins a near tie
CROSS_SECS = 0.015        # the crossfade that closes the loop
# A loop may only start where the voice's harmonic profile holds until the longest note ends: the
# profile (harmonics 1-8 at the note's own frequency, as fractions of their sum) of every later
# window may differ from the start's by this much (sum of the fractions' differences, 0..2) per
# dB of flat_db.  The level settling is not enough: Spring Yard's $05 (FM3) is flat within 1 dB
# from 20 ms while its operators' detune swings its second harmonic from -23 to +6 dB over 300 ms,
# and a loop at 32 ms froze its first instant
PROFILE_PER_DB = 0.25
PROFILE_HARMONICS = 8
PROFILE_HOP_SECS = 0.010      # a profile every 10 ms
PROFILE_SPAN_SECS = 1.0       # each start is checked this far ahead (or to the longest note's end)
MAX_ERROR = 0.75          # the largest raw discontinuity a loop is still made from
SILENT_DB = -50.0         # a sustain end this far below the peak has decayed: nothing to loop
MAX_STARTS = 6            # loop starts tried, one fundamental period apart, from the flat point
PROBE_SECS = 4.0          # sustain rendered to look for a loop in (span + longest loop + attack)
RELEASE_FLOOR_DB = 48.0   # a release this far down is at the 8-bit floor (~49 dB): nothing past it is heard
MAX_END_FRACTION = 0.8    # a loop ending later than this fraction of the sustain saves nothing:
                          # the envelope was still settling (a slowly decaying voice) - no loop
MIN_DECAY_DB_S = 0.1      # a fall slower than this (dB per second) is no fall: a plain loop


@dataclass(slots=True)
class SustainLoop:
    """A loop found in a rendered sample: play [0, start + length), then loop [start, start + length)."""
    start: int          # sample index, even
    length: int         # samples, even
    error: float        # relative RMS discontinuity at the loop point before the crossfade
    flat_at: int        # first sample of the flat region
    cross: int = 0      # samples crossfaded at the loop's end (apply_loop)
    decay_db: float = 0.0   # dB the level falls per sample from flat_at on (find_sustain_loop's
                            # `decay`): flattened in the sample, written as volume slides

    @property
    def end(self) -> int:
        return self.start + self.length


def _rms(x: Sequence[float]) -> float:
    return math.sqrt(sum(v * v for v in x) / len(x)) if len(x) else 0.0


def _db(v: float, ref: float) -> float:
    return gain_to_db(v / ref) if v > 0 and ref > 0 else -120.0


def _even(n: float) -> int:
    return round(n / 2.0) * 2


def loop_error(x: Sequence[float], start: int, length: int, span: int) -> float:
    """The discontinuity a loop [start, start + length) introduces: the RMS difference between
    the `span` samples after the loop start and those after its end, relative to the signal."""
    a = x[start:start + span]
    b = x[start + length:start + length + span]
    num = sum((p - q) * (p - q) for p, q in zip(a, b, strict=True))
    den = sum(p * p for p in a)
    return math.sqrt(num / den) if den > 0 else math.inf


def _errors(x: Sequence[float], s: int, min_len: int, max_len: int, check: int,
            period: float, cycle: float = 0.0) -> list[tuple[float, int]]:
    """[(error, length)] for the even loop lengths in [min_len, max_len] at start `s`: every
    one with numpy, else the fundamental's grid (k periods, two samples either way).  With an
    LFO `cycle`, only the lengths within a period of a whole number of cycles."""
    if cycle:
        return [(loop_error(x, s, length, check), length) for length in range(min_len, max_len + 1, 2)
                if abs(length - max(1, round(length / cycle)) * cycle) <= period]
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        seg = np.asarray(x[s:s + max_len + check], dtype=np.float64)
        a = seg[:check]
        den = float(a @ a)
        if den <= 0:
            return []
        win = np.lib.stride_tricks.sliding_window_view(seg, check)   # row L = the window at s + L
        d = win - a
        err = np.sqrt(np.einsum("ij,ij->i", d, d) / den)
        lengths = np.arange(min_len, max_len + 1, 2)
        return [(float(err[L]), int(L)) for L in lengths if len(err) > L]
    out = []
    k = 1
    while True:
        base = _even(k * period)
        if base > max_len:
            break
        out.extend((loop_error(x, s, length, check), length)
                   for length in (base - 2, base, base + 2) if min_len <= length <= max_len)
        k += 1
    return out


def _band(ref: list[float], tol: float) -> tuple[float, float]:
    """The band a window must sit in to count as flat: within `tol` of the reference span's value
    at its END, widened by whatever the span swings around its trend (a beating pair).  Detrended,
    so a decaying voice's span does not pass its whole decay - and its attack - as flat: the loop
    may only freeze a level `tol` above where the longest note would have ended, which is what
    loop_drift_db promises."""
    n_ref = len(ref)
    mt = (n_ref - 1) / 2.0
    mean = sum(ref) / n_ref
    sxx = sum((i - mt) ** 2 for i in range(n_ref))
    slope = sum((i - mt) * (v - mean) for i, v in enumerate(ref)) / sxx if sxx else 0.0
    end = mean + slope * (n_ref - 1 - mt)
    residual = [v - (mean + slope * (i - mt)) for i, v in enumerate(ref)]
    return end + min(residual) - tol, end + max(residual) + tol


def _trend_flat(env: list[float], ref_w: int, span_w: int, tol: float) -> tuple[int, float]:
    """(first window of the straight fall, its slope in dB per window): the reference span's
    trend line extended back, every window from the first one on within `tol` of it (widened by
    the span's own swing around it, a beating pair), and the slope refitted over all of them."""
    ref = env[ref_w - span_w:ref_w]
    mt = (span_w - 1) / 2.0
    mean = sum(ref) / span_w
    sxx = sum((i - mt) ** 2 for i in range(span_w))
    slope = sum((i - mt) * (v - mean) for i, v in enumerate(ref)) / sxx if sxx else 0.0
    residual = [v - (mean + slope * (i - mt)) for i, v in enumerate(ref)]
    lo, hi = min(residual) - tol, max(residual) + tol
    flat = 0
    for i in range(ref_w - 1, -1, -1):
        r = env[i] - (mean + slope * (i - (ref_w - span_w) - mt))
        if not lo <= r <= hi:
            flat = i + 1
            break
    pts = env[flat:ref_w]
    n = len(pts)
    if n < 2:
        return flat, slope
    m = (n - 1) / 2.0
    mean = sum(pts) / n
    sxx = sum((i - m) ** 2 for i in range(n))
    return flat, sum((i - m) * (v - mean) for i, v in enumerate(pts)) / sxx


def flatten(mono: Sequence[float], start: int, decay_db: float) -> list[float]:
    """The samples from `start` on turned up by `decay_db` dB per sample: a level that falls in a
    straight line (in dB) held where it was at `start`."""
    out = list(mono)
    for i in range(start, len(out)):
        out[i] *= 10.0 ** (decay_db * (i - start) / 20.0)
    return out


def _profile(window: Sequence[float], period: float) -> list[float]:
    """Harmonics 1..PROFILE_HARMONICS of a window at the note's frequency (Goertzel), as fractions
    of their sum: its timbre, whatever its level or phase."""
    mags = []
    for h in range(1, PROFILE_HARMONICS + 1):
        w = 2 * math.pi * h / period
        if w >= math.pi:
            break
        coeff, s1, s2 = 2 * math.cos(w), 0.0, 0.0
        for v in window:
            s1, s2 = v + coeff * s1 - s2, s1
        mags.append(math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)))
    total = sum(mags)
    return [m / total for m in mags] if total else mags


def _profile_holds_from(mono: Sequence[float], rate: int, period: float, start: int, until: int,
                        tol: float) -> int:
    """The first sample from `start` (on a PROFILE_HOP_SECS grid) whose harmonic profile every
    later one stays within `tol` of until `until` (the longest note's end), PROFILE_SPAN_SECS at
    most ahead; `until` when none does (a loop then only plays under the release)."""
    hop = max(1, round(PROFILE_HOP_SECS * rate))
    size = max(16, round(2 * period))
    span = round(PROFILE_SPAN_SECS * rate)
    stop = min(until, len(mono) - size)
    if start >= stop:
        return start
    profiles = {}

    def at(i: int) -> list[float]:
        if i not in profiles:
            profiles[i] = _profile(mono[i:i + size], period)
        return profiles[i]

    for s in range(start, stop, hop):
        ref = at(s)
        if all(sum(abs(a - b) for a, b in zip(ref, at(c), strict=True)) <= tol
               for c in range(s + hop, min(stop, s + span), hop)):
            return s
    return until


def find_sustain_loop(mono: Sequence[float], rate: int, period: float, sustain_n: int, *,
                      ref_n: int | None = None, max_end: int | None = None,
                      flat_db: float = FLAT_DB, span_secs: float = SPAN_SECS,
                      min_loop_secs: float = MIN_LOOP_SECS, max_loop_secs: float = MAX_LOOP_SECS,
                      cross_secs: float = CROSS_SECS, max_error: float = MAX_ERROR,
                      timbre: bool = True, decay: bool = False,
                      min_start_secs: float = 0.0, cycle: float = 0.0) -> SustainLoop | None:
    """A sustain loop for a render of a note held for `sustain_n` samples at `rate` Hz whose
    fundamental period is `period` samples, or None where the envelope never settles (a
    decaying voice, one that has decayed to silence, or a sustain too short to judge).

    `ref_n` is where the reference span ends (default: the sustain's end): the envelope is
    flat from the first window that stays within `flat_db` of the span's range, so with
    `ref_n` at the end of the longest note the instrument plays, a decaying voice may still
    loop, frozen at a level at most `flat_db` above where that note would have ended - the
    settings' loop_drift_db.  `max_end` (default: MAX_END_FRACTION of the sustain) is the
    latest sample a loop may end at: later, it saves nothing over the plain render.

    `timbre` also asks the voice's harmonic profile to hold from the loop's start to the longest
    note's end (PROFILE_PER_DB); the merged build turns it off, as it costs bytes there
    (SampleSettings.loop_timbre).

    `decay` (loop_decay: slide): flat means falling in a straight line (in dB) within `flat_db`
    of the reference span's trend, the timbre check always on; the loop is searched in the
    render flattened from there (`flatten`) and carries the fall (SustainLoop.decay_db), which
    apply_loop flattens the same way.

    `min_start_secs` (loop_start_ms): no loop starts earlier, wherever the envelope settles - a
    beating pair is flat from its first window (its swing spans the band), and a loop that starts
    inside the attack replays a piece of it on every pass.

    `cycle` (samples): the period of the hardware LFO the voice plays under; 0: none.

    The loop is not yet closed: apply_loop crossfades it and cuts the sample.
    """
    n = min(len(mono), sustain_n)
    if period <= 0 or n <= 0:
        return None
    win = max(math.ceil(2 * period), 64, math.ceil(cycle))
    timbre = timbre and not cycle
    nwin = n // win
    span_w = max(4, int(span_secs * rate / win))
    if nwin < span_w + 4:
        return None
    # The reference span ends at the longest note's end; when that note is shorter than the
    # span, it is the span after the note's end instead (never the attack)
    ref_w = (ref_n if ref_n is not None else n) // win
    if ref_w < span_w:
        ref_w += span_w
    ref_w = min(nwin, max(ref_w, span_w))
    peak = max((abs(v) for v in mono[:n]), default=0)
    if peak <= 0:
        return None
    env = [_db(_rms(mono[i * win:(i + 1) * win]), peak) for i in range(nwin)]
    if max(env[ref_w - span_w:ref_w]) < SILENT_DB:
        return None                                  # decayed away before the reference
    decay_db = 0.0
    if decay:
        flat, slope = _trend_flat(env, ref_w, span_w, flat_db)
        if -slope * rate / win >= MIN_DECAY_DB_S:
            decay_db = -slope / win
        timbre = True
    else:
        lo, hi = _band(env[ref_w - span_w:ref_w], flat_db)
        # From the reference span's end back, the span's own windows included: a decaying voice's
        # span sits above the band at its start, and a loop there froze that level (the Title
        # Screen's voice $01 looped at 0.2 s, 7 dB above where its longest note ends)
        flat = 0
        for i in range(ref_w - 1, -1, -1):
            if not lo <= env[i] <= hi:
                flat = i + 1
                break
    flat_at = max(_even(flat * win), _even(min_start_secs * rate))
    if timbre:
        flat_at = _even(_profile_holds_from(mono, rate, period, flat_at, ref_n if ref_n is not None else n,
                                            PROFILE_PER_DB * flat_db))
    end_limit = int(MAX_END_FRACTION * n) if max_end is None else min(max_end, n)
    x = flatten(mono[:n], flat_at, decay_db) if decay_db else mono

    check = max(32, math.ceil(2 * period))
    cross = _even(cross_secs * rate)
    min_len = max(4, _even(min_loop_secs * rate), 2 * cross, _even(cycle - period))
    max_len = max(_even(max_loop_secs * rate), _even(cycle + period))
    step = max(2, _even(period))
    best: SustainLoop | None = None
    best_score = math.inf
    for j in range(MAX_STARTS):
        s = max(flat_at, cross) + j * step
        top = min(max_len, n - s - check, end_limit - s)
        if top < min_len:
            break
        for e, length in _errors(x, s, min_len, top, check, period, cycle):
            # Bytes before the loop cost as much as bytes in it; the merged build keeps its old score
            score = e + LENGTH_PENALTY * (length + (s - flat_at if timbre else 0)) / rate
            if score < best_score:
                best_score = score
                best = SustainLoop(s, length, e, flat_at, min(cross, length // 2), decay_db)
    if best is None or best.error > max_error:
        return None
    return best


def probe_secs(sustain: float, fits: float) -> float:
    """The sustain to render to look for a loop in: PROBE_SECS (long enough to see an envelope
    settle), no shorter than `sustain`, no longer than `fits` (the sample limit's)."""
    return min(max(sustain, PROBE_SECS), fits)


def apply_loop(mono: Sequence[float], loop: SustainLoop) -> list[float]:
    """The sample cut at the loop's end, with the last `loop.cross` samples of the loop faded
    into the samples before the loop's start so playback jumps back without a step; a sliding
    loop's samples flattened from its flat point on first."""
    if loop.decay_db:
        mono = flatten(mono[:loop.end], loop.flat_at, loop.decay_db)
    out = list(mono[:loop.end])
    c = loop.cross
    if c > 0 and loop.start >= c:
        for i in range(c):
            w = (i + 1) / (c + 1)
            j = loop.end - c + i
            out[j] = out[j] * (1.0 - w) + mono[loop.start - c + i] * w
    return out


def release_rate_db_s(mono: Sequence[float], rate: int, keyoff_n: int, period: float,
                      floor_db: float = -40.0) -> float | None:
    """How fast the level falls after key-off, in dB per second (a least-squares slope over
    the envelope's windows from key-off until it is `floor_db` below the level at key-off).

    None where there is nothing to release (the level at key-off is already down at the
    silence floor, a percussive voice) or too little tail to measure; math.inf where the
    release is over within a window; 0.0 where the level does not fall (release rate 0:
    the hardware rings on).
    """
    if period <= 0 or keyoff_n <= 0 or keyoff_n >= len(mono):
        return None
    win = max(math.ceil(2 * period), 64)
    peak = max((abs(v) for v in mono), default=0)
    if peak <= 0 or keyoff_n < win:
        return None
    ref = _db(_rms(mono[keyoff_n - win:keyoff_n]), peak)
    if ref < SILENT_DB:
        return None
    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    i = 0
    while keyoff_n + (i + 1) * win <= len(mono):
        d = _db(_rms(mono[keyoff_n + i * win:keyoff_n + (i + 1) * win]), peak) - ref
        t = (i + 1) * win / rate
        if d < floor_db or d < SILENT_DB - ref:
            pts.append((t, max(d, floor_db)))
            break
        pts.append((t, d))
        i += 1
    if len(pts) < 3:
        # Fewer than two windows before the floor: the release is over within a window
        return math.inf if len(pts) == 2 and pts[1][1] <= floor_db else None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    md = sum(d for _, d in pts) / n
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    if sxx <= 0:
        return None
    slope = sum((t - mt) * (d - md) for t, d in pts) / sxx
    return max(0.0, -slope)


def heard_padding(padding: float, release_db_s: float | None, slides: bool) -> float:
    """Seconds of a sample past its sustain a note can still be heard, for a sample whose
    sustain holds every note: none where the converter cuts notes at their end (C00), else
    the release slide's fall to the 8-bit floor (the voice's release rate), at most `padding`."""
    if not slides or release_db_s is None or not math.isfinite(release_db_s) or release_db_s <= 0:
        return 0.0
    return min(padding, RELEASE_FLOOR_DB / release_db_s)


def fade_end(mono: Sequence[float], n: int, rate: int) -> list[float]:
    """The first `n` samples, the last 2 ms faded out (the end no note reaches)."""
    out = list(mono[:n])
    fade = min(len(out), max(1, int(rate * 0.002)))
    for i in range(fade):
        out[len(out) - fade + i] *= 1 - (i + 1) / fade
    return out


def unroll_values(values, loop: tuple[int, int], length: int) -> list:
    """`unroll` for a render's values (floats or ints) instead of its bytes."""
    start, ln = loop
    if ln <= 0 or start + ln > len(values):
        return list(values[:length])
    body = list(values[start:start + ln])
    out = list(values[:start + ln])
    while len(out) < length:
        out += body
    return out[:length]

