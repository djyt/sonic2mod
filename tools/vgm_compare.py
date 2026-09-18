#!/usr/bin/env python3
"""Rendered per-channel comparison of a converted MOD against its VGM/VGZ reference.

Renders the reference VGZ one chip channel at a time (VGMPlay with mute masks)
and the converted MOD one MOD channel at a time (ffmpeg + libopenmpt on
channel-isolated copies), then lines the two up and reports, per channel:

  * every key-on event: reference pitch, MOD pitch error in cents, level in
    both renders and the level difference
  * level balance of each channel relative to a reference channel
  * onset timing deviations (MOD grid vs. driver tempo jitter, lost notes)
  * noise: onset list, decay envelope, spectral band profile (LFSR rate check)
  * DAC: per-hit low-frequency peak (playback-rate check) and band profile

Requirements: numpy, ffmpeg with the libopenmpt demuxer on PATH, and a VGMPlay
directory (VGMPlay64.exe / VGMPlay.exe + VGMPlay.ini + zlib1.dll).  Pass it
with --vgmplay or set the VGMPLAY_DIR environment variable.

Usage::

    python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz"
    python tools/vgm_compare.py configs/01_title_screen.yaml ref.vgz --mod output/x.mod --ref FM2
    python tools/vgm_compare.py cfg.yaml ref.vgz --skip-render     # reuse WAVs in the workdir
    python tools/vgm_compare.py cfg.yaml ref.vgz --offset 0.25     # force MOD-minus-VGM offset (s)

Renders land in --workdir (default: output/compare/<config-name>/).
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("ERROR: numpy is required (pip install numpy)", file=sys.stderr)
    sys.exit(1)

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig
from tools.vgm_analyze import _parse_vgm

SR = 44100
_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# VGM channel name -> (YM2612 mute mask, SN76496 mute mask) that leaves ONLY that channel audible.
_YM_ALL, _SN_ALL = 0x7F, 0xF
_VGM_CHANNELS = {
    "FM1": (_YM_ALL & ~0x01, _SN_ALL), "FM2": (_YM_ALL & ~0x02, _SN_ALL),
    "FM3": (_YM_ALL & ~0x04, _SN_ALL), "FM4": (_YM_ALL & ~0x08, _SN_ALL),
    "FM5": (_YM_ALL & ~0x10, _SN_ALL), "FM6": (_YM_ALL & ~0x20, _SN_ALL),
    "DAC": (_YM_ALL & ~0x40, _SN_ALL),
    "PSG1": (_YM_ALL, _SN_ALL & ~0x1), "PSG2": (_YM_ALL, _SN_ALL & ~0x2),
    "PSG3": (_YM_ALL, _SN_ALL & ~0x4), "NOISE": (_YM_ALL, _SN_ALL & ~0x8),
}
_MOD_FORMAT_CHANNELS = {"M.K.": 4, "M!K!": 4, "6CHN": 6, "8CHN": 8, "10CH": 10, "12CH": 12, "16CH": 16}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _find_vgmplay(arg: str | None) -> Path:
    cand = arg or os.environ.get("VGMPLAY_DIR")
    if not cand:
        raise SystemExit("ERROR: VGMPlay directory not given (--vgmplay DIR or VGMPLAY_DIR env var)")
    d = Path(cand)
    for exe in ("VGMPlay64.exe", "VGMPlay.exe", "vgmplay"):
        if (d / exe).exists():
            return d
    raise SystemExit(f"ERROR: no VGMPlay executable found in {d}")


def _patch_ini(src: str, ym_mask: int, sn_mask: int, core: str) -> str:
    out, section = [], None
    for line in src.splitlines():
        m = re.match(r"^\[(.+)\]", line)
        if m:
            section = m.group(1)
            out.append(line)
            if section == "YM2612":
                out += [f"MuteMask = 0x{ym_mask:02X}", f"Core = {core}"]
            elif section == "SN76496":
                out.append(f"MuteMask = 0x{sn_mask:X}")
            continue
        if section == "General" and re.match(r"^\s*(LogSound|MaxLoops|SampleRate)\s*=", line):
            continue
        if section in ("YM2612", "SN76496") and re.match(r"^\s*(MuteMask|Core|MuteCh\d)\s*=", line):
            continue
        out.append(line)
    text = "\n".join(out) + "\n"
    # Force WAV logging, one pass, 44100 Hz.
    text = text.replace("[General]", "[General]\nLogSound = 1\nMaxLoops = 1\nSampleRate = 44100", 1)
    return text


def render_vgm_channels(vgz: Path, names: list[str], vgmplay: Path, outdir: Path, core: str) -> None:
    exe = next(e for e in ("VGMPlay64.exe", "VGMPlay.exe", "vgmplay") if (vgmplay / e).exists())
    base_ini = (vgmplay / "VGMPlay.ini").read_text(encoding="utf-8", errors="replace")
    outdir.mkdir(parents=True, exist_ok=True)
    for name in ["FULL", *names]:
        ym, sn = (0x00, 0x0) if name == "FULL" else _VGM_CHANNELS[name]
        work = outdir / f"_vgm_{name}"
        work.mkdir(exist_ok=True)
        for f in vgmplay.iterdir():
            if f.suffix.lower() in (".exe", ".dll"):
                shutil.copy(f, work / f.name)
        (work / "VGMPlay.ini").write_text(_patch_ini(base_ini, ym, sn, core), encoding="utf-8")
        shutil.copy(vgz, work / "ref.vgz")
        subprocess.run([str(work / exe), "ref.vgz"], cwd=work, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=False)
        wav = work / "ref.wav"
        if not wav.exists():
            raise SystemExit(f"ERROR: VGMPlay produced no WAV for {name} (check LogSound support)")
        shutil.move(str(wav), str(outdir / f"vgm_{name}.wav"))
        shutil.rmtree(work, ignore_errors=True)
        print(f"  rendered VGM {name}")


def _isolate_mod(data: bytes, keep_ch: int | None) -> bytes:
    """Return a copy of the MOD with every channel except keep_ch stripped of notes.

    Global flow effects (Fxx speed/tempo, Bxx jump, Dxx break) are kept on all
    channels so timing and song structure are unchanged.
    """
    if keep_ch is None:
        return data
    b = bytearray(data)
    nch = _MOD_FORMAT_CHANNELS.get(data[1080:1084].decode("ascii", "replace"), 4)
    positions = data[952:952 + data[950]]
    npat = (max(positions) + 1) if positions else 0
    for p in range(npat):
        for r in range(64):
            for c in range(nch):
                if c == keep_ch:
                    continue
                off = 1084 + (p * 64 + r) * nch * 4 + c * 4
                eff = b[off + 2] & 0x0F
                if eff in (0xF, 0xB, 0xD):
                    b[off], b[off + 1], b[off + 2] = 0, 0, eff
                else:
                    b[off:off + 4] = b"\0\0\0\0"
    return bytes(b)


def render_mod_channels(mod_path: Path, channels: dict[str, int], outdir: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ERROR: ffmpeg not found on PATH")
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=libopenmpt"],
                           capture_output=True, text=True, check=False)
    if "libopenmpt" not in probe.stdout:
        raise SystemExit("ERROR: this ffmpeg build has no libopenmpt demuxer")
    data = mod_path.read_bytes()
    outdir.mkdir(parents=True, exist_ok=True)
    for name, ch in [("FULL", None), *channels.items()]:
        iso = outdir / f"_mod_{name}.mod"
        iso.write_bytes(_isolate_mod(data, ch))
        wav = outdir / f"mod_{name}.wav"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "libopenmpt",
                            "-sample_rate", str(SR), "-i", str(iso), "-ar", str(SR), "-ac", "2", str(wav)],
                           capture_output=True, text=True, check=False)
        iso.unlink(missing_ok=True)
        if r.returncode != 0 or not wav.exists():
            raise SystemExit(f"ERROR: ffmpeg failed for MOD channel {name}:\n{r.stderr}")
        print(f"  rendered MOD {name}")


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), 'rb') as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2 or sr != SR:
        raise SystemExit(f"ERROR: {path} must be 16-bit {SR} Hz (got {sw * 8}-bit {sr} Hz)")
    a = np.frombuffer(raw, dtype='<i2').astype(np.float64) / 32768.0
    return a.reshape(-1, ch).mean(axis=1) if ch > 1 else a


def db(x: float) -> float:
    return 20 * math.log10(max(x, 1e-9))


def rms(seg: np.ndarray) -> float:
    return float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0


def seg_at(a: np.ndarray, t: float, dur: float) -> np.ndarray:
    s, e = int(t * SR), int((t + dur) * SR)
    if s < 0 or s >= len(a):
        return np.zeros(max(e - s, 1))
    return a[s:e]


def note_name(f: float) -> str:
    if f <= 0:
        return "---"
    m = 69 + 12 * math.log2(f / 440.0)
    mi = round(m)
    return f"{_NOTE_NAMES[mi % 12]}{mi // 12 - 1}"


def cents(f: float, ref: float) -> float:
    return 1200 * math.log2(f / ref) if f > 0 and ref > 0 else float('nan')


def spectrum(seg: np.ndarray, nfft: int) -> tuple[np.ndarray, np.ndarray]:
    mag = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), nfft))
    return np.fft.rfftfreq(nfft, 1 / SR), mag


def peak_near(seg: np.ndarray, f0: float, semis: float = 0.75) -> float:
    """Interpolated spectral peak within +/- semis of f0 (0.0 if the window is empty)."""
    if len(seg) < 64:
        return 0.0
    nfft = 1 << max(15, math.ceil(math.log2(len(seg) * 4)))
    freqs, mag = spectrum(seg, nfft)
    lo, hi = f0 / 2 ** (semis / 12), f0 * 2 ** (semis / 12)
    idx = np.where((freqs >= lo) & (freqs <= hi))[0]
    if len(idx) == 0:
        return 0.0
    k = idx[np.argmax(mag[idx])]
    d = 0.0
    if 1 <= k < len(mag) - 1:
        a, b, c = (np.log(mag[k - 1] + 1e-12), np.log(mag[k] + 1e-12), np.log(mag[k + 1] + 1e-12))
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if den != 0 else 0.0
    return float((k + d) * SR / nfft)


def band_profile(seg: np.ndarray, bands: list[tuple[int, int]], fmax: float = 22050) -> list[float]:
    freqs, mag = spectrum(seg, 8192)
    p = mag ** 2
    tot = p[freqs < fmax].sum() + 1e-12
    return [10 * math.log10(p[(freqs >= a) & (freqs < b)].sum() / tot + 1e-12) for a, b in bands]


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


def auto_offset(vgm_full: np.ndarray, mod_full: np.ndarray, max_lag: float = 3.0) -> float:
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


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(x: float, w: int = 7, p: int = 1) -> str:
    return f"{x:>{w}.{p}f}" if not math.isnan(x) else f"{'nan':>{w}}"


def _hz(f: int) -> str:
    return f"{f // 1000}k" if f >= 1000 and f % 1000 == 0 else str(f)


def report(cfg: ConversionConfig, vgz: Path, mod_path: Path, workdir: Path,
           offset: float | None, ref_chan: str, max_rows: int) -> None:
    raw = gzip.decompress(vgz.read_bytes()) if vgz.read_bytes()[:2] == b'\x1f\x8b' else vgz.read_bytes()
    rows, _, _ = _parse_vgm(raw, 7_670_454, 3_579_545, None, 'all')
    events = [r for r in rows if r[1] != "DAC"]

    chan_map = {c.source: c.mod_channel for c in cfg.channels if c.source in _VGM_CHANNELS or c.source == "PSG3"}
    # PSG3 in noise mode is the NOISE channel of the chip; tone mode is PSG3.  Detect from events.
    noise_used = any(r[1] == "NOISE" for r in rows)
    names = []
    for src in chan_map:
        if src == "PSG3" and noise_used:
            names.append("NOISE")
        else:
            names.append(src)
    vgm = {n: load_wav(workdir / f"vgm_{n}.wav") for n in ["FULL", *names]}
    mod = {}
    for src, n in zip(chan_map, names, strict=True):
        mod[n] = load_wav(workdir / f"mod_{src}.wav")
    mod["FULL"] = load_wav(workdir / "mod_FULL.wav")

    if offset is None:
        offset = auto_offset(vgm["FULL"], mod["FULL"])
        print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (auto, envelope cross-correlation)")
    else:
        print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (given)")
    print()

    # ---- per-note pitch and level (FM + PSG tone) ----
    per_ch: dict[str, list] = {}
    for r in events:
        if r[1] in vgm and r[1] != "NOISE" and r[4] > 0:
            per_ch.setdefault(r[1], []).append(r)
    level_diff: dict[str, list[float]] = {}
    cent_err: dict[str, list[float]] = {}
    printed = 0
    if per_ch:
        print("Per-note comparison (levels are dBFS of the isolated channel; diff = MOD - VGM)")
        print(f"{'chan':<6}{'t_vgm':>7}  {'ref':<4}{'ref_hz':>8}  {'vgm_c':>6}  {'mod_c':>6}  {'vgm_dB':>7}  {'mod_dB':>7}  {'diff':>6}")
        print("-" * 78)
    for ch, evs in per_ch.items():
        for i, r in enumerate(evs):
            t, _, _, _, fref, note, _ = r
            t /= 1000.0
            t_end = evs[i + 1][0] / 1000.0 if i + 1 < len(evs) else t + 2.0
            dur = t_end - t
            win = min(0.3 if fref < 200 else 0.1, max(0.04, dur - 0.03))
            sv = seg_at(vgm[ch], t + 0.025, win)
            sm = seg_at(mod[ch], t + offset + 0.025, win)
            cv = cents(peak_near(sv, fref), fref)
            cm = cents(peak_near(sm, fref), fref)
            lv, lm = db(rms(sv)), db(rms(sm))
            level_diff.setdefault(ch, []).append(lm - lv)
            if not math.isnan(cm):
                cent_err.setdefault(ch, []).append(cm)
            flag = "  <-- PITCH" if (math.isnan(cm) or abs(cm) > 25) else ""
            if lm < -70:
                flag = "  <-- SILENT in MOD"
            if max_rows <= 0 or printed < max_rows or flag:
                print(f"{ch:<6}{t:>7.3f}  {note:<4}{fref:>8.1f}  {_fmt(cv, 6)}  {_fmt(cm, 6)}  "
                      f"{lv:>7.1f}  {lm:>7.1f}  {lm - lv:>6.1f}{flag}")
                printed += 1
    if per_ch:
        print()
        print("Per-channel summary")
        print(f"{'chan':<6}{'notes':>6}  {'pitch err cents (median/min/max)':<34}  {'level diff dB (median)':<22}")
        for ch in per_ch:
            ce = cent_err.get(ch, [float('nan')])
            ld = level_diff.get(ch, [float('nan')])
            print(f"{ch:<6}{len(per_ch[ch]):>6}  {statistics.median(ce):>8.1f} / {min(ce):>6.1f} / {max(ce):>6.1f}"
                  f"{'':<8}  {statistics.median(ld):>+8.1f}")
        print()

    # ---- channel balance relative to reference channel ----
    ref = ref_chan if ref_chan in vgm else names[0]
    print(f"Whole-song channel RMS relative to {ref} (dB):   VGM     MOD    MOD-VGM")
    rv_ref, rm_ref = db(rms(vgm[ref])), db(rms(mod[ref]))
    for n in names:
        rv, rm = db(rms(vgm[n])) - rv_ref, db(rms(mod[n])) - rm_ref
        note = "" if abs(rm - rv) < 2 else "   <-- rebalance"
        print(f"  {n:<6} {rv:>8.1f} {rm:>8.1f} {rm - rv:>+8.1f}{note}")
    print(f"  (absolute: VGM mix {db(rms(vgm['FULL'])):.1f} dBFS peak {np.abs(vgm['FULL']).max():.2f};"
          f" MOD mix {db(rms(mod['FULL'])):.1f} dBFS peak {np.abs(mod['FULL']).max():.2f})")
    print()

    # ---- onset timing ----
    # Both sides use the same audio onset detector so slow instrument attacks cancel out.
    # For channels with key-on events the VGM detector hit is anchored to each event.
    print("Onset timing (VGM onset -> nearest MOD onset, MOD shifted by the alignment offset)")
    for n in names:
        thr = -50 if n == "NOISE" else -40
        vdet = onsets(vgm[n], thresh_db=thr)
        if n in per_ch:
            vo = []
            for r in per_ch[n]:
                t = r[0] / 1000.0
                hits = [x for x in vdet if t - 0.005 <= x <= t + 0.06]
                vo.append(hits[0] if hits else t)
        else:
            vo = vdet
        mo = [t - offset for t in onsets(mod[n], thresh_db=thr)]
        devs, missing = [], 0
        for t in vo:
            if not mo:
                missing += 1
                continue
            j = min(range(len(mo)), key=lambda k: abs(mo[k] - t))
            d = (mo[j] - t) * 1000
            if abs(d) > 40:
                missing += 1
            else:
                devs.append(d)
        if devs:
            print(f"  {n:<6} {len(vo):3d} ref onsets, {len(mo):3d} MOD onsets; matched {len(devs)}: "
                  f"median {statistics.median(devs):+.0f} ms, worst {max(devs, key=abs):+.0f} ms; "
                  f"unmatched {missing}")
        else:
            print(f"  {n:<6} {len(vo):3d} ref onsets, {len(mo):3d} MOD onsets; none matched")
    print()

    # ---- noise ----
    if "NOISE" in names:
        vo = onsets(vgm["NOISE"], thresh_db=-50)
        mo = onsets(mod["NOISE"], thresh_db=-50)
        print(f"NOISE: {len(vo)} ref hits, {len(mo)} MOD hits")
        steps = [0.0, 0.017, 0.033, 0.05, 0.067, 0.083, 0.1, 0.133, 0.167, 0.2, 0.25]
        print("  decay envelope (dB at ms after onset): " + ' '.join(f"{int(s * 1000):>5d}" for s in steps))
        for k in sorted({0, 1, len(vo) - 1} & set(range(len(vo)))):
            ev = [db(rms(seg_at(vgm["NOISE"], vo[k] + s, 0.02))) for s in steps]
            em = [db(rms(seg_at(mod["NOISE"], vo[k] + offset + s, 0.02))) for s in steps]
            print(f"  hit {k:2d} VGM  " + ' '.join(f"{x:>5.0f}" for x in ev))
            print("         MOD  " + ' '.join(f"{x:>5.0f}" for x in em))
        bands = [(0, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 13000)]
        if vo:
            sv = seg_at(vgm["NOISE"], vo[0] + 0.005, 0.04)
            sm = seg_at(mod["NOISE"], vo[0] + offset + 0.005, 0.04)
            print("  band energy dB rel total <13 kHz: " + ' '.join(f"{_hz(a)}-{_hz(b)}" for a, b in bands))
            print("     VGM " + ' '.join(f"{x:>7.1f}" for x in band_profile(sv, bands, 13000)))
            print("     MOD " + ' '.join(f"{x:>7.1f}" for x in band_profile(sm, bands, 13000)))
            print("  (a MOD profile that falls off above 4 kHz while VGM is flat means the LFSR"
                  " clock (tone2_n / synth_root) is too low)")
        print()

    # ---- DAC ----
    if "DAC" in names:
        vo = onsets(vgm["DAC"], thresh_db=-40, hold=0.08)
        mo = onsets(mod["DAC"], thresh_db=-40, hold=0.08)
        print(f"DAC: {len(vo)} ref hits, {len(mo)} MOD hits (first {min(6, len(vo))} shown)")
        bands = [(0, 150), (150, 300), (300, 600), (600, 1200), (1200, 2400), (2400, 4800), (4800, 9600), (9600, 22050)]
        print("  hit   t_vgm  lowpeak_vgm lowpeak_mod   band dB VGM / MOD: " + ' '.join(f"{_hz(a)}-{_hz(b)}" for a, b in bands))
        for k in range(min(6, len(vo))):
            t = vo[k]
            sv = seg_at(vgm["DAC"], t + 0.005, 0.08)
            sm = seg_at(mod["DAC"], t + offset + 0.005, 0.08)
            fv, mv = spectrum(sv, 32768)
            fm_, mm = spectrum(sm, 32768)
            lo = (fv > 40) & (fv < 400)
            pv = fv[lo][np.argmax(mv[lo])]
            pm = fm_[lo][np.argmax(mm[lo])]
            print(f"  {k:3d} {t:>7.3f} {pv:>11.1f} {pm:>11.1f}   "
                  + ' '.join(f"{x:.0f}" for x in band_profile(sv, bands)))
            print(f"  {'':3} {'':7} {'':11} {'':11}   "
                  + ' '.join(f"{x:.0f}" for x in band_profile(sm, bands)))
        print("  (lowpeak differing by more than ~3% means the DAC sample plays at the wrong rate)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="song YAML config (channel mapping, output_file)")
    ap.add_argument("vgz", help="reference VGM/VGZ recording of the same song")
    ap.add_argument("--mod", help="MOD to compare (default: config output_file)")
    ap.add_argument("--vgmplay", help="VGMPlay directory (default: VGMPLAY_DIR env var)")
    ap.add_argument("--workdir", help="where rendered WAVs go (default: output/compare/<config name>/)")
    ap.add_argument("--offset", type=float, help="MOD-minus-VGM time offset in seconds (default: auto)")
    ap.add_argument("--skip-render", action="store_true", help="reuse WAVs already in the workdir")
    ap.add_argument("--ref", default="FM2", help="reference channel for balance table (default FM2)")
    ap.add_argument("--core", default="NUKE", help="VGMPlay YM2612 core: NUKE (default), GPGX, GENS")
    ap.add_argument("--max-rows", type=int, default=400, help="per-note rows to print (flagged rows always print)")
    args = ap.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    cfg = ConversionConfig.from_yaml(args.config)
    mod_path = Path(args.mod or cfg.output_file)
    vgz = Path(args.vgz)
    for p in (mod_path, vgz):
        if not p.exists():
            raise SystemExit(f"ERROR: file not found: {p}")
    workdir = Path(args.workdir or Path("output") / "compare" / Path(args.config).stem)

    chan_map = {c.source: c.mod_channel for c in cfg.channels}
    raw = gzip.decompress(vgz.read_bytes()) if vgz.read_bytes()[:2] == b'\x1f\x8b' else vgz.read_bytes()
    rows, _, _ = _parse_vgm(raw, 7_670_454, 3_579_545, None, 'all')
    noise_used = any(r[1] == "NOISE" for r in rows)
    vgm_names = ["NOISE" if (s == "PSG3" and noise_used) else s for s in chan_map]
    vgm_names = [n for n in vgm_names if n in _VGM_CHANNELS]

    if not args.skip_render:
        vgmplay = _find_vgmplay(args.vgmplay)
        print(f"Rendering reference channels with {vgmplay} ...")
        render_vgm_channels(vgz, vgm_names, vgmplay, workdir, args.core)
        print("Rendering MOD channels with ffmpeg/libopenmpt ...")
        render_mod_channels(mod_path, chan_map, workdir)
        print()

    print(f"Config : {args.config}")
    print(f"MOD    : {mod_path}")
    print(f"VGZ    : {vgz}")
    print(f"Renders: {workdir}")
    print()
    report(cfg, vgz, mod_path, workdir, args.offset, args.ref, args.max_rows)


if __name__ == "__main__":
    main()
