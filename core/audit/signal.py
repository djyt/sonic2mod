"""Measures on the renders: WAV loading, level, spectra, partials, onsets, the envelope
alignment, pitch tracks and vibrato."""

from __future__ import annotations

import itertools
import math
import statistics
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ..audio import cents, db_to_gain, gain_to_db, power_to_db
from .render import SR, workers


def load_wav(path: Path, stereo: bool = False) -> np.ndarray:
    """Mono mix (L+R)/2 by default; with stereo=True the (frames, channels) array.

    Pitch, onsets and envelopes use the mono mix.  LEVELS must use the stereo array: rms() of it
    is the power average of both sides, which is what a hard-panned channel actually delivers.
    Averaging to mono first reads a hard-panned YM2612 channel ~5 dB low against a centred one
    (GHZ FM4/FM5), while every libopenmpt MOD channel loses the same ~1 dB, so the error does
    not cancel between the two renders.
    """
    with wave.open(str(path), 'rb') as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2 or sr != SR:
        raise SystemExit(f"ERROR: {path} must be 16-bit {SR} Hz (got {sw * 8}-bit {sr} Hz)")
    a = np.frombuffer(raw, dtype='<i2').astype(np.float64) / 32768.0
    a = a.reshape(-1, ch)
    return a if stereo else a.mean(axis=1)


def db(x: float) -> float:
    return gain_to_db(max(x, 1e-9))


def rms(seg: np.ndarray) -> float:
    return float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0


def seg_at(a: np.ndarray, t: float, dur: float) -> np.ndarray:
    s, e = int(t * SR), int((t + dur) * SR)
    if s < 0 or s >= len(a):
        return np.zeros((max(e - s, 1), *a.shape[1:]))
    return a[s:e]


def spectrum(seg: np.ndarray, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    mag = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), nfft))
    return np.fft.rfftfreq(nfft, 1 / SR), mag


def _band_peak(freqs: np.ndarray, mag: np.ndarray, f0: float, semis: float) -> tuple[float, float]:
    """(interpolated peak Hz, its magnitude) within +/- semis of f0; (0, 0) when the band is empty."""
    lo, hi = f0 / 2 ** (semis / 12), f0 * 2 ** (semis / 12)
    idx = np.where((freqs >= lo) & (freqs <= hi))[0]
    if len(idx) == 0:
        return 0.0, 0.0
    k = idx[np.argmax(mag[idx])]
    d = 0.0
    if 1 <= k < len(mag) - 1:
        a, b, c = (np.log(mag[k - 1] + 1e-12), np.log(mag[k] + 1e-12), np.log(mag[k + 1] + 1e-12))
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if den != 0 else 0.0
    return float((k + d) * freqs[1]), float(mag[k])


def harmonic_cents(seg: np.ndarray, f0: float, harmonic: int | None = None, semis: float = 0.75) -> tuple[int, float]:
    """(harmonic used, cents that partial is from harmonic * f0).

    An FM voice whose carriers run at a frequency multiple of 2 or more has no energy at the
    channel's register frequency, and a peak search there only finds leakage.  With `harmonic`
    None the strongest of the first four partials is used; pass that number back in to measure
    the other render on the same partial.
    """
    if len(seg) < 64:
        return harmonic or 1, float("nan")
    nfft = 1 << max(15, math.ceil(math.log2(len(seg) * 4)))
    freqs, mag = spectrum(seg, nfft)
    cands = [harmonic] if harmonic else [k for k in (1, 2, 3, 4) if k * f0 < SR / 2.5]
    k, (f, _) = max(((k, _band_peak(freqs, mag, k * f0, semis)) for k in cands), key=lambda c: c[1][1])
    return k, cents(f, k * f0)


def band_profile(seg: np.ndarray, bands: list[tuple[int, int]], fmax: float = 22050) -> list[float]:
    freqs, mag = spectrum(seg, 8192)
    p = mag ** 2
    tot = p[freqs < fmax].sum() + 1e-12
    return [power_to_db(p[(freqs >= a) & (freqs < b)].sum() / tot + 1e-12) for a, b in bands]


def envelope_db(a: np.ndarray, frame: float = 0.005) -> np.ndarray:
    n = int(frame * SR)
    nf = len(a) // n
    if nf == 0:
        return np.array([-120.0])
    x = a[:nf * n].reshape(nf, n)
    return 20 * np.log10(np.sqrt((x * x).mean(axis=1)) + 1e-9)


def onsets(a: np.ndarray, thresh_db: float = -45, frame: float = 0.005, hold: float = 0.03,
           rise_db: float = 6) -> list[float]:
    env = envelope_db(a, frame)
    out: list[float] = []
    last = -1.0
    for i in range(2, len(env)):
        if env[i] > thresh_db and env[i] - min(env[i - 2], env[i - 1]) > rise_db:
            t = i * frame
            if last < 0 or t - last > hold:
                out.append(t)
            last = t
    return out


def envelope_offset(vgm_full: np.ndarray, mod_full: np.ndarray, max_lag: float = 3.0) -> float:
    """MOD-minus-VGM time offset (s) that best aligns the two mix envelopes."""
    frame = 0.005
    ev = np.clip(envelope_db(vgm_full, frame), -60, 0)
    em = np.clip(envelope_db(mod_full, frame), -60, 0)
    ev -= ev.mean()
    em -= em.mean()
    n = min(len(ev), len(em))
    ev, em = ev[:n], em[:n]
    maxl = int(max_lag / frame)
    best, best_lag = -1e18, 0
    for lag in range(-maxl, maxl + 1):
        c = float(np.dot(em[lag:], ev[:n - lag])) if lag >= 0 else float(np.dot(em[:n + lag], ev[-lag:]))
        if c > best:
            best, best_lag = c, lag
    return best_lag * frame




VIB_MIN_NOTE = 0.5          # seconds; shorter notes do not hold enough cycles to measure
_VIB_FRAME = 0.005           # pitch-track frame (200 Hz)
_VIB_BAND = (2.5, 14.0)      # plausible vibrato rates, Hz
_VIB_MIN_DEPTH = 3.0         # cents; below this it is period-table / FNUM quantisation wobble
_VIB_MIN_R2 = 0.35           # share of pitch-track variance a sinusoid at the rate must explain
_VIB_BEAT_AM = 0.15          # level swing (fraction of mean) at the same rate that marks beating
_PEAK_FLOOR_DB = -25         # dB under the strongest spectral peak below which _pick_partial counts none


def _pick_partial(seg: np.ndarray) -> tuple[float, float]:
    """(centre Hz, half-bandwidth Hz) of the partial that is easiest to isolate; (0, 0) if none.

    FM voices with fractional operator multiples put partials on a lattice finer than the note's
    own frequency (Title Screen voice $01 at A2: 55 / 82.5 / 110 Hz), so the spacing is measured
    rather than assumed.  It is read off the peaks below 500 Hz, where a vibrato of a few tens of
    cents is too narrow to show up as separate sideband peaks.
    """
    freqs, mag = spectrum(seg, 1 << math.ceil(math.log2(len(seg) * 2)))
    keep = (freqs >= 40) & (freqs <= 4000)
    freqs, mag = freqs[keep], mag[keep].copy()
    if not len(mag) or mag.max() <= 0:
        return 0.0, 0.0
    floor = mag.max() * db_to_gain(_PEAK_FLOOR_DB)
    peaks: list[tuple[float, float]] = []            # (Hz, magnitude), strongest first
    work = mag.copy()
    while len(peaks) < 24:
        k = int(np.argmax(work))
        if work[k] < floor:
            break
        peaks.append((float(freqs[k]), float(work[k])))
        work[np.abs(freqs - freqs[k]) < 12.0] = 0
    low = sorted(f for f, _ in peaks if f < 500) or sorted(f for f, _ in peaks)
    gaps = [b - a for a, b in itertools.pairwise(low)]
    spacing = min(gaps) if gaps else low[0]
    # A partial needs its own swing (2 % covers +/-35 cents) plus the first modulation sidebands.
    ok = [(f, m) for f, m in peaks if 0.02 * f + 8.0 <= 0.45 * spacing]
    fc = max(ok, key=lambda x: x[1])[0] if ok else min(f for f, _ in peaks)
    return fc, min(0.45 * spacing, 3 * (0.02 * fc + 8.0))


def pitch_track(seg: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """(pitch deviation in cents per _VIB_FRAME, NaN where quiet; the partial's level per frame;
    filter half-bandwidth in Hz).

    Partial tracking by heterodyne: one partial is shifted to DC, isolated with a brick-wall
    low-pass narrower than the partial spacing, and the phase derivative of what is left is its
    instantaneous frequency.  Every partial of an FM or PSG note moves by the same number of
    cents, so which one is tracked does not matter.
    """
    n = len(seg)
    if n < int(0.2 * SR):
        return np.array([]), np.array([]), 0.0
    fc, bw = _pick_partial(seg)
    if fc <= 0:
        return np.array([]), np.array([]), 0.0

    t = np.arange(n) / SR
    spec = np.fft.fft(seg * np.exp(-2j * np.pi * fc * t))
    spec[np.abs(np.fft.fftfreq(n, 1 / SR)) > bw] = 0
    z = np.fft.ifft(spec)

    hop = int(_VIB_FRAME * SR)
    nf = (n - 1) // hop
    if nf < 8:
        return np.array([]), np.array([]), 0.0
    # Amplitude-weighted mean frequency per frame: sum(z[k+1]·conj(z[k])) has the mean phase step
    # as its angle and ignores samples where the partial has faded out.
    prod = (z[1:] * np.conj(z[:-1]))[:nf * hop].reshape(nf, hop).sum(axis=1)
    amp = np.abs(z[:nf * hop]).reshape(nf, hop).mean(axis=1)
    dev_hz = np.angle(prod) * SR / (2 * np.pi)
    track = 1200 * np.log2(np.maximum(fc + dev_hz, 1e-6) / fc)
    track[amp < 0.1 * amp.max()] = np.nan
    edge = int(0.03 / _VIB_FRAME)          # brick-wall filter rings at the segment ends
    track[:edge] = np.nan
    track[-edge:] = np.nan
    return track, amp, bw


def vibrato_estimates(segs: list[np.ndarray]) -> list[dict | None]:
    """vibrato_estimate of every segment, in order, several at once: numpy's FFTs release the GIL,
    and a song has hundreds of long notes to measure in two renders each."""
    with ThreadPoolExecutor(max_workers=workers(len(segs))) as pool:
        return list(pool.map(vibrato_estimate, segs))


def vibrato_estimate(seg: np.ndarray) -> dict | None:
    """Rate (Hz) and depth (+/- cents) of periodic pitch modulation in seg; None if there is none.

    Only the modulated stretch of the note is measured, so a delayed onset (smpsModSet wait) or a
    4xy that stops before the note does not dilute the figures.  The depth is the median of the
    per-cycle extremes, which is the same for the driver's triangle and ProTracker's sine and
    shrugs off the pitch glitches at attack and release.
    """
    track, amp, bw = pitch_track(seg)
    ok = np.where(~np.isnan(track))[0]
    trim = int(0.05 / _VIB_FRAME)                     # attack / release transients
    if len(ok) < int(0.4 / _VIB_FRAME) + 2 * trim:
        return None
    first = int(ok[0]) + trim
    track = track[first:int(ok[-1]) + 1 - trim]
    amp = amp[first + 1:int(ok[-1]) - trim]            # aligned with the smoothed track below
    if np.isnan(track).any():
        idx = np.arange(len(track))
        good = ~np.isnan(track)
        track = np.interp(idx, idx[good], track[good])
    track = np.convolve(track, np.ones(3) / 3, mode="same")[1:-1]     # 15 ms smoothing

    # Local swing = half the pitch range inside a window one slowest-vibrato cycle long.  The
    # modulated stretch is the longest run where it stays near its typical (75th percentile)
    # value; far above that is a glitch, far below is an unmodulated part of the note.
    win = int(1 / _VIB_BAND[0] / _VIB_FRAME)
    if len(track) < win + int(0.2 / _VIB_FRAME):
        return None
    views = np.lib.stride_tricks.sliding_window_view(track, win)
    local = (views.max(axis=1) - views.min(axis=1)) / 2
    typical = float(np.percentile(local, 75))
    if typical < _VIB_MIN_DEPTH:
        return None
    active = (local > 0.5 * typical) & (local < 2.5 * typical)
    best_a = best_b = run_a = 0
    for i, on in enumerate([*active, False]):
        if on:
            continue
        if i - run_a > best_b - best_a:
            best_a, best_b = run_a, i
        run_a = i + 1
    a, b = best_a, best_b + win - 1                   # window index -> frame span
    if (b - a) * _VIB_FRAME < 0.3:
        return None
    x = track[a:b]
    tt = np.arange(len(x)) * _VIB_FRAME
    x = x - np.polyval(np.polyfit(tt, x, 1), tt)

    nfft = 8192
    mag = np.abs(np.fft.rfft(x * np.hanning(len(x)), nfft))
    fr = np.fft.rfftfreq(nfft, _VIB_FRAME)
    # Anything within 20 % of the brick-wall edge is a neighbouring partial beating through the
    # filter skirt, not modulation.
    band = np.where((fr >= _VIB_BAND[0]) & (fr <= min(_VIB_BAND[1], 0.8 * bw)))[0]
    k = int(band[np.argmax(mag[band])])
    rate = float(fr[k])
    if 1 <= k < len(mag) - 1:
        p, q, r = mag[k - 1], mag[k], mag[k + 1]
        den = p - 2 * q + r
        if den != 0:
            rate = float((k + 0.5 * (p - r) / den) * fr[1])

    basis = np.column_stack([np.sin(2 * np.pi * rate * tt), np.cos(2 * np.pi * rate * tt)])
    coef, *_ = np.linalg.lstsq(basis, x, rcond=None)
    # FNUM / period vibrato moves the pitch and leaves the level alone.  Two detuned FM carriers
    # beating also wobble a partial's phase periodically, but they swing its level at the same
    # rate — and that rate scales with sample playback speed in the MOD, which vibrato does not.
    lvl = amp[a:b]
    lvl = lvl / lvl.mean() - 1 if len(lvl) == len(x) and lvl.mean() > 0 else np.zeros(len(x))
    lvl = lvl - np.polyval(np.polyfit(tt, lvl, 1), tt)
    am_coef, *_ = np.linalg.lstsq(basis, lvl, rcond=None)
    am_depth = float(np.hypot(*am_coef))
    var = float(np.var(x))
    r2 = 1 - float(np.var(x - basis @ coef)) / var if var > 0 else 0.0
    cycle = max(2, round(1 / rate / _VIB_FRAME))
    cycles = [x[i:i + cycle] for i in range(0, len(x) - cycle + 1, cycle)]
    if len(cycles) < 2:
        return None
    depth = (statistics.median(float(c.max()) for c in cycles)
             - statistics.median(float(c.min()) for c in cycles)) / 2
    if r2 < _VIB_MIN_R2 or depth < _VIB_MIN_DEPTH:
        return None
    # Onset: the stretch above is only located to within one analysis window, so take the first
    # frame of the whole track that strays 40 % of the depth from the pitch the note starts at.
    # (Reads ~0.1 cycle late, equally on both renders.)
    rest = float(np.median(track[:max(3, int(0.05 / _VIB_FRAME))]))
    moved = np.where(np.abs(track[:b] - rest) > 0.4 * depth)[0]
    onset = int(moved[0]) if len(moved) else a
    return {"rate_hz": rate, "depth_cents": depth, "onset_s": (first + 1 + onset) * _VIB_FRAME,
            "dur_s": (b - a) * _VIB_FRAME, "r2": r2, "am_depth": am_depth,
            "kind": "beat" if am_depth >= _VIB_BEAT_AM else "vibrato"}
