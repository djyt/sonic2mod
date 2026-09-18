#!/usr/bin/env python3
"""Rendered per-channel comparison of a converted MOD against its VGM/VGZ reference.

Renders the reference VGZ one chip channel at a time (VGMPlay with mute masks)
and the converted MOD one MOD channel at a time (ffmpeg + libopenmpt on
channel-isolated copies), then lines the two up and reports, per channel:

  * every key-on event: reference pitch, MOD pitch error in cents, level in
    both renders and the level difference
  * level balance of each channel relative to a reference channel
  * level error per MOD instrument (grouped by channel and Cxx), with the sample_list volume that
    would zero it; --write-volumes applies those to the config
  * onset timing deviations (MOD grid vs. driver tempo jitter, lost notes)
  * vibrato on long notes: rate (Hz) and depth (+/- cents) in both renders
  * noise: onset list, decay envelope, spectral band profile (LFSR rate check)
  * DAC: per-hit low-frequency peak (playback-rate check) and band profile

Requirements: numpy, ffmpeg with the libopenmpt demuxer on PATH, and a VGMPlay
0.51.x directory (VGMPlay64.exe / VGMPlay.exe + VGMPlay.ini + zlib1.dll; the
libvgm-based line, whose VGMPlay.ini selects cores with ``Core = NUKE``).  It is
looked up in this order: --vgmplay DIR, the VGMPLAY_DIR environment variable,
then reference/vgz/vgmplay/ (untracked, like the rest of reference/vgz/ -- unzip
a VGMPlay build there on a fresh checkout; see docs/pipeline.md).

Usage::

    python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz"
    python tools/vgm_compare.py configs/01_title_screen.yaml ref.vgz --mod output/x.mod --ref FM2
    python tools/vgm_compare.py cfg.yaml ref.vgz --skip-render     # reuse WAVs in the workdir
    python tools/vgm_compare.py cfg.yaml ref.vgz --offset 0.25     # force MOD-minus-VGM offset (s)

    # CI-style: machine-readable results + non-zero exit when a threshold is exceeded
    python tools/vgm_compare.py cfg.yaml ref.vgz --json output/compare/title.json --fail-balance-db 2

Renders land in --workdir (default: output/compare/<config-name>/).
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import itertools
import json
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
from tools import vgm_pitch_audit
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

_VGMPLAY_EXES = ("VGMPlay64.exe", "VGMPlay.exe", "vgmplay")
_VGMPLAY_DEFAULT = _HERE.parent / "reference" / "vgz" / "vgmplay"


def _find_vgmplay(arg: str | None) -> Path:
    """Resolve the VGMPlay directory: --vgmplay, then VGMPLAY_DIR, then reference/vgz/vgmplay/."""
    cand = arg or os.environ.get("VGMPLAY_DIR")
    d = Path(cand) if cand else _VGMPLAY_DEFAULT
    if any((d / exe).exists() for exe in _VGMPLAY_EXES):
        return d
    if cand:
        raise SystemExit(f"ERROR: no VGMPlay executable found in {d}")
    raise SystemExit(
        f"ERROR: VGMPlay not found in {_VGMPLAY_DEFAULT}\n"
        "       Unzip a VGMPlay 0.51.x build there (the directory is untracked), or pass\n"
        "       --vgmplay DIR / set VGMPLAY_DIR.  See 'VGM comparison setup' in docs/pipeline.md.")


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
    exe = next(e for e in _VGMPLAY_EXES if (vgmplay / e).exists())
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
    return 20 * math.log10(max(x, 1e-9))


def rms(seg: np.ndarray) -> float:
    return float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0


def seg_at(a: np.ndarray, t: float, dur: float) -> np.ndarray:
    s, e = int(t * SR), int((t + dur) * SR)
    if s < 0 or s >= len(a):
        return np.zeros((max(e - s, 1), *a.shape[1:]))
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
# Per-instrument levels
# ---------------------------------------------------------------------------

def mod_note_events(mod: bytes, speed: int) -> tuple[dict[int, list[tuple]], dict[int, tuple[str, int]], float]:
    """({channel: [(time s, instrument, Cxx value or None)]}, {instrument: (name, volume)}, length s).

    Follows Bxx / Dxx and stops at the song loop, like the player does on one pass.
    """
    nch = _MOD_FORMAT_CHANNELS.get(mod[1080:1084].decode("ascii", "replace"), 4)
    order = list(mod[952:952 + mod[950]])
    samples = {}
    for i in range(31):
        h = mod[20 + 30 * i:50 + 30 * i]
        if int.from_bytes(h[22:24], "big"):
            samples[i + 1] = (h[:22].split(b"\0")[0].decode("ascii", "replace"), h[25])
    events: dict[int, list[tuple]] = {c: [] for c in range(nch)}
    bpm, now, posi, row = 125, 0.0, 0, 0
    seen: set[tuple[int, int]] = set()
    while posi < len(order) and (posi, row) not in seen:
        seen.add((posi, row))
        base = 1084 + (order[posi] * 64 + row) * nch * 4
        jump = brk = None
        for c in range(nch):
            b = mod[base + c * 4:base + c * 4 + 4]
            period, ins, eff, par = ((b[0] & 15) << 8) | b[1], (b[0] & 0xF0) | (b[2] >> 4), b[2] & 15, b[3]
            if eff == 0xF and par:
                bpm, speed = (par, speed) if par >= 0x20 else (bpm, par)
            elif eff == 0xB:
                jump = par
            elif eff == 0xD:
                brk = (par >> 4) * 10 + (par & 15)
            if period and ins:
                events[c].append((now, ins, par if eff == 0xC else None))
        now += speed * 2.5 / bpm
        if jump is not None:
            if jump <= posi:
                break
            posi, row = jump, brk or 0
        elif brk is not None:
            posi, row = posi + 1, brk
        else:
            row += 1
            if row == 64:
                posi, row = posi + 1, 0
    return events, samples, now


_LEVEL_SPAN = 0.6            # seconds of a note that count towards its level
_LEVEL_MIN_NOTES = 4         # fewer plain notes than this and no volume is suggested
_LEVEL_MAX_SPREAD = 3.0      # dB between channels sharing an instrument before it is "not a volume problem"
_LEVEL_DAC_SLACK = 2.0       # dB the DAC must be BELOW the song's median before everything is balanced to it
_LEVEL_MAX_ERR = 18.0        # dB; beyond this something other than the volume is wrong (silent / wrong instrument)


def instrument_levels(note_times: dict[str, list[float]], mod_chan: dict[str, int], events: dict[int, list[tuple]],
                      samples: dict[int, tuple[str, int]], mod_end: float, vgm_st: dict, mod_st: dict,
                      offset: float) -> dict:
    """Level error MOD - VGM per (chip channel, MOD instrument, Cxx), and per instrument.

    Errors are relative to an anchor so the unknown gain between the two renders drops out.
    The anchor is the median over every plain (no Cxx) synthesised note, so that only RELATIVE
    imbalance is reported — unless the DAC is QUIETER than that median by _LEVEL_DAC_SLACK dB or
    more.  DAC samples sit at volume 64 and cannot be turned up, so then everything else has to
    come down to meet them.  A DAC that is too LOUD is simply turned down (it gets a suggestion
    like any instrument), and a gap under the slack is within what short DAC hits can be measured
    to — chasing it would rewrite every volume for nothing.
    Only notes without a Cxx say what the instrument's own volume should be.
    """
    groups: dict[tuple, list[float]] = {}
    for ch, times in note_times.items():
        evs = events.get(mod_chan[ch], [])
        short = ch in ("DAC", "NOISE")
        for i, t in enumerate(times):
            if t > mod_end - 0.3:
                break
            dur = min((times[i + 1] - t) if i + 1 < len(times) else _LEVEL_SPAN, 0.15 if ch == "DAC" else _LEVEL_SPAN)
            if dur < (0.04 if short else 0.09):
                continue
            lv = db(rms(seg_at(vgm_st[ch], t, dur)))
            lm = db(rms(seg_at(mod_st[ch], t + offset, dur)))
            if lv < -55 or lm < -75:
                continue
            hit = None
            for e in evs:
                if e[0] > t + offset + 0.03:
                    break
                hit = e
            if hit is not None:
                groups.setdefault((ch, hit[1], hit[2]), []).append(lm - lv)

    def med(keys) -> float | None:
        xs = [x for k in keys for x in groups[k]]
        return statistics.median(xs) if xs else None

    dac = med(k for k in groups if k[0] == "DAC")
    n_dac = sum(len(groups[k]) for k in groups if k[0] == "DAC")
    song = med(k for k in groups if k[0] != "DAC" and k[2] is None)
    if song is None:
        song = med(k for k in groups if k[0] != "DAC")
    if song is None:
        return {"anchor": None, "groups": [], "instruments": []}
    dac_vs_song = (dac - song) if dac is not None and n_dac >= 8 else None
    anchor: float
    if dac is not None and dac_vs_song is not None and dac_vs_song <= -_LEVEL_DAC_SLACK:
        anchor_name, anchor = f"the DAC (which is {dac_vs_song:+.1f} dB against the rest of the song)", dac
    else:
        anchor = song
        anchor_name = "the song's median note" + (
            f" (DAC {dac_vs_song:+.1f} dB against it)" if dac_vs_song is not None else "")

    out_groups = [{"channel": ch, "instrument": ins, "cxx": cxx, "notes": len(xs),
                   "err_db": statistics.median(xs) - anchor}
                  for (ch, ins, cxx), xs in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2] or -1))]
    instruments = []
    for ins in sorted({g["instrument"] for g in out_groups}):
        plain = [g for g in out_groups if g["instrument"] == ins and g["cxx"] is None]
        if not plain:
            continue
        notes = sum(g["notes"] for g in plain)
        err = statistics.median(x for g in plain for x in groups[(g["channel"], ins, None)]) - anchor
        spread = max(g["err_db"] for g in plain) - min(g["err_db"] for g in plain)
        name, vol = samples.get(ins, ("?", 64))
        ok = notes >= _LEVEL_MIN_NOTES and spread <= _LEVEL_MAX_SPREAD and abs(err) <= _LEVEL_MAX_ERR
        instruments.append({
            "instrument": ins, "name": name, "volume": vol, "notes": notes, "err_db": err, "spread_db": spread,
            "channels": sorted({g["channel"] for g in plain}),
            "wanted": vol * 10 ** (-err / 20) if ok else None,
        })
    scale = suggest_volumes(instruments)
    return {"anchor": anchor_name, "anchor_db": anchor, "groups": out_groups, "instruments": instruments,
            "scaled_db": 20 * math.log10(scale)}


def suggest_volumes(instruments: list[dict]) -> float:
    """Fill in `suggested` from `wanted`; returns the common scale applied (1.0 = none).

    64 is the ceiling.  An instrument that wants a little more than that (within the slack that a
    level can be measured to — typically a DAC sample already at 64 reading a hair quiet) is
    simply clamped.  Only when one wants substantially more are ALL suggestions brought down
    together, so that the balance between them survives.
    """
    wanted = [it["wanted"] for it in instruments if it.get("wanted") is not None]
    ceiling = 64.0 * 10 ** (_LEVEL_DAC_SLACK / 20)
    scale = min(1.0, ceiling / max(wanted)) if wanted else 1.0
    for it in instruments:
        it["suggested"] = None if it.get("wanted") is None else max(1, min(64, round(it["wanted"] * scale)))
    return scale


def write_volumes(config_path: Path, instruments: list[dict], min_db: float = 1.0) -> list[str]:
    """Set sample_list volumes to the suggested values; returns a line per change."""
    text = config_path.read_text(encoding="utf-8")
    changes = []
    for it in instruments:
        new = it["suggested"]
        if new is None or new == it["volume"] or abs(20 * math.log10(new / it["volume"])) < min_db:
            continue
        pat = re.compile(r'^(\s*-\s*\[\s*' + str(it["instrument"])
                         + r'\s*,\s*"[^"]*"\s*,\s*)(\d+)(\s*,\s*-?\d+\s*\])([^\r\n]*)', re.M)
        m = pat.search(text)
        if not m or int(m.group(2)) != it["volume"]:
            changes.append(f"  !! instrument {it['instrument']}: no sample_list line with volume {it['volume']} — not changed")
            continue
        note = f"VGZ: {it['err_db']:+.1f} dB at {it['volume']}"
        tail = re.sub(r"\s*;?\s*VGZ: [^;]*", "", m.group(4)).rstrip()
        tail = f"{tail}; {note}" if tail.strip().startswith("#") and tail.strip() != "#" else f" # {note}"
        text = text[:m.start()] + f"{m.group(1)}{new:>{len(m.group(2))}}{m.group(3)}{tail}" + text[m.end():]
        changes.append(f"  instrument {it['instrument']:>2} ({it['name']}): {it['volume']} -> {new}  ({it['err_db']:+.1f} dB)")
    if any(not c.startswith("  !!") for c in changes):
        config_path.write_text(text, encoding="utf-8", newline="")
    return changes


# ---------------------------------------------------------------------------
# Vibrato
# ---------------------------------------------------------------------------

_VIB_MIN_NOTE = 0.5          # seconds; shorter notes do not hold enough cycles to measure
_VIB_FRAME = 0.005           # pitch-track frame (200 Hz)
_VIB_BAND = (2.5, 14.0)      # plausible vibrato rates, Hz
_VIB_MIN_DEPTH = 3.0         # cents; below this it is period-table / FNUM quantisation wobble
_VIB_MIN_R2 = 0.35           # share of pitch-track variance a sinusoid at the rate must explain
_VIB_BEAT_AM = 0.15          # level swing (fraction of mean) at the same rate that marks beating


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
    floor = mag.max() * 10 ** (-25 / 20)
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


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(x: float, w: int = 7, p: int = 1) -> str:
    return f"{x:>{w}.{p}f}" if not math.isnan(x) else f"{'nan':>{w}}"


def _hz(f: int) -> str:
    return f"{f // 1000}k" if f >= 1000 and f % 1000 == 0 else str(f)


def report(cfg: ConversionConfig, vgz: Path, mod_path: Path, workdir: Path,
           offset: float | None, ref_chan: str, max_rows: int) -> dict:
    """Print the comparison and return the same numbers as a JSON-serialisable dict."""
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
    # *_st: stereo arrays, used for every level figure (see load_wav); vgm / mod: mono mixes.
    vgm_st = {n: load_wav(workdir / f"vgm_{n}.wav", stereo=True) for n in ["FULL", *names]}
    mod_st = {}
    for src, n in zip(chan_map, names, strict=True):
        mod_st[n] = load_wav(workdir / f"mod_{src}.wav", stereo=True)
    mod_st["FULL"] = load_wav(workdir / "mod_FULL.wav", stereo=True)
    vgm = {n: a.mean(axis=1) for n, a in vgm_st.items()}
    mod = {n: a.mean(axis=1) for n, a in mod_st.items()}

    offset_auto = offset is None
    if offset is None:
        # Note starts from the register log against the MOD's note rows: exact, and immune to the
        # envelope method's failure on sparse or tempo-drifting songs (Chaos Emerald +920 ms,
        # Drowning +1205 ms).  The envelope correlation is kept for songs with no pitched notes.
        chip_tl, _ = vgm_pitch_audit.chip_timeline(raw)
        mod_tl, _ = vgm_pitch_audit.mod_timeline(mod_path.read_bytes(), cfg)
        if any(mod_tl.values()):
            offset = vgm_pitch_audit.auto_offset(chip_tl, mod_tl, {c.source: c.mod_channel for c in cfg.channels})
            env = auto_offset(vgm["FULL"], mod["FULL"])
            print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (auto, note starts"
                  + (f"; envelope correlation says {env * 1000:+.0f} ms — ignored)" if abs(env - offset) > 0.03 else ")"))
        else:
            offset = auto_offset(vgm["FULL"], mod["FULL"])
            print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (auto, envelope cross-correlation)")
    else:
        print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (given)")
    print()
    res: dict = {
        "offset_ms": offset * 1000, "offset_auto": offset_auto,
        "channels": {n: {} for n in names}, "notes": [], "vibrato": [],
    }

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
            lv = db(rms(seg_at(vgm_st[ch], t + 0.025, win)))
            lm = db(rms(seg_at(mod_st[ch], t + offset + 0.025, win)))
            level_diff.setdefault(ch, []).append(lm - lv)
            if not math.isnan(cm):
                cent_err.setdefault(ch, []).append(cm)
            flag = "  <-- PITCH" if (math.isnan(cm) or abs(cm) > 25) else ""
            if lm < -70:
                flag = "  <-- SILENT in MOD"
            res["notes"].append({
                "channel": ch, "t_s": t, "note": note, "ref_hz": fref, "vgm_cents": cv, "mod_cents": cm,
                "vgm_db": lv, "mod_db": lm, "diff_db": lm - lv, "silent_in_mod": lm < -70,
            })
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
            res["channels"][ch].update({
                "notes": len(per_ch[ch]),
                "pitch_cents": {"median": statistics.median(ce), "min": min(ce), "max": max(ce)},
                "level_diff_db_median": statistics.median(ld),
            })
        print()

    # ---- vibrato on long notes ----
    # Key-off is not in the event list, so a note runs to the next key-on on its channel and
    # pitch_track() drops the frames where it has already faded.
    long_notes = 0
    vib_rows: list[str] = []
    for ch, evs in per_ch.items():
        for i, r in enumerate(evs):
            t, note = r[0] / 1000.0, r[5]
            dur = (evs[i + 1][0] / 1000.0 - t) if i + 1 < len(evs) else 3.0
            if dur < _VIB_MIN_NOTE:
                continue
            long_notes += 1
            span = min(dur, 4.0) - 0.02
            vv = vibrato_estimate(seg_at(vgm[ch], t + 0.01, span))
            vm = vibrato_estimate(seg_at(mod[ch], t + offset + 0.01, span))
            if vv is None and vm is None:
                continue
            if vv is None or vm is None:
                flag = "  <-- MISSING in MOD" if vm is None else "  <-- not in VGM"
            elif "beat" in (vv["kind"], vm["kind"]):
                flag = "  <-- BEAT RATE" if abs(vm["rate_hz"] / vv["rate_hz"] - 1) > 0.15 else ""
            elif (abs(vm["rate_hz"] / vv["rate_hz"] - 1) > 0.15
                  or abs(vm["depth_cents"] - vv["depth_cents"]) > max(5.0, 0.3 * vv["depth_cents"])):
                flag = "  <-- VIBRATO"
            else:
                flag = ""

            def _vib(v: dict | None) -> str:
                if not v:
                    return f"{'none':>18}"
                return f"{v['rate_hz']:>5.2f} Hz +/-{v['depth_cents']:>4.1f} c{'b' if v['kind'] == 'beat' else ' '}"
            vib_rows.append(f"{ch:<6}{t:>7.3f}  {note:<4}{dur:>6.2f}   {_vib(vv)}   {_vib(vm)}{flag}")
            res["vibrato"].append({"channel": ch, "t_s": t, "note": note, "dur_s": dur,
                                   "vgm": vv, "mod": vm, "mismatch": bool(flag)})
    if per_ch:
        print(f"Vibrato ({long_notes} notes of {_VIB_MIN_NOTE} s or longer checked; rows = notes that modulate in either render)")
        if vib_rows:
            print(f"{'chan':<6}{'t_vgm':>7}  {'ref':<4}{'dur':>6}   {'VGM rate / depth':>18}   {'MOD rate / depth':>18}")
            print("-" * 68)
            for row in vib_rows:
                print(row)
            print("  (4xy: rate = x*(speed-1)*BPM/(160*speed) Hz; depth grows with y and with the note's period)")
            print("  (b = beating of detuned FM carriers, not smpsModSet: the level swings at the same rate."
                  "  Its rate follows")
            print("   sample playback speed, so a BEAT RATE mismatch points at synth_root / multi-sampling,"
                  " not at 4xy)")
        else:
            print("  none found")
        print()

    # ---- channel balance relative to reference channel ----
    ref = ref_chan if ref_chan in vgm else names[0]
    print(f"Whole-song channel RMS relative to {ref} (dB, L/R power):   VGM     MOD    MOD-VGM")
    rv_ref, rm_ref = db(rms(vgm_st[ref])), db(rms(mod_st[ref]))
    for n in names:
        rv, rm = db(rms(vgm_st[n])) - rv_ref, db(rms(mod_st[n])) - rm_ref
        note = "" if abs(rm - rv) < 2 else "   <-- rebalance"
        print(f"  {n:<6} {rv:>8.1f} {rm:>8.1f} {rm - rv:>+8.1f}{note}")
        res["channels"][n]["balance_db"] = {"vgm": rv, "mod": rm, "diff": rm - rv}
    res["ref_channel"] = ref
    res["mix"] = {"vgm_rms_db": db(rms(vgm_st["FULL"])), "vgm_peak": float(np.abs(vgm_st["FULL"]).max()),
                  "mod_rms_db": db(rms(mod_st["FULL"])), "mod_peak": float(np.abs(mod_st["FULL"]).max())}
    print(f"  (absolute: VGM mix {db(rms(vgm_st['FULL'])):.1f} dBFS peak {np.abs(vgm_st['FULL']).max():.2f};"
          f" MOD mix {db(rms(mod_st['FULL'])):.1f} dBFS peak {np.abs(mod_st['FULL']).max():.2f})")
    print()

    # ---- per-instrument levels ----
    note_times = {ch: [r[0] / 1000.0 for r in evs] for ch, evs in per_ch.items()}
    if "NOISE" in names:
        note_times["NOISE"] = [r[0] / 1000.0 for r in rows if r[1] == "NOISE"]
    if "DAC" in names:
        note_times["DAC"] = onsets(vgm["DAC"], thresh_db=-40, hold=0.08)
    src_of = dict(zip(names, chan_map, strict=True))
    events_by_chan, samples, mod_end = mod_note_events(mod_path.read_bytes(), cfg.target_speed)
    lev = instrument_levels(note_times, {n: chan_map[src_of[n]] for n in note_times}, events_by_chan, samples,
                            mod_end, vgm_st, mod_st, offset)
    res["instrument_levels"] = lev
    if lev["instruments"]:
        print(f"Per-instrument level error, MOD - VGM, relative to {lev['anchor']} (dB).  Notes without a Cxx set the volume;")
        print("channels sharing an instrument should agree (spread) — if they do not, it is not a volume problem.")
        print(f"{'inst':>4} {'sample':<22} {'vol':>3} {'notes':>5} {'err':>6} {'spread':>6} {'suggest':>7}   per channel (Cxx: err xnotes)")
        for it in lev["instruments"]:
            parts = []
            for g in lev["groups"]:
                if g["instrument"] == it["instrument"]:
                    cxx = "" if g["cxx"] is None else f" C{g['cxx']:02X}"
                    parts.append(f"{g['channel']}{cxx}: {g['err_db']:+.1f} x{g['notes']}")
            detail = "  ".join(parts)
            sug = "" if it["suggested"] is None else ("=" if it["suggested"] == it["volume"] else str(it["suggested"]))
            flag = "  <-- channels disagree" if it["spread_db"] > _LEVEL_MAX_SPREAD else ""
            if abs(it["err_db"]) > _LEVEL_MAX_ERR:
                flag = "  <-- too far off to be a volume problem"
            print(f"{it['instrument']:>4} {it['name']:<22} {it['volume']:>3} {it['notes']:>5} {it['err_db']:>+6.1f} "
                  f"{it['spread_db']:>6.1f} {sug:>7}   {detail}{flag}")
        if lev["scaled_db"] < -0.05:
            print(f"  (suggestions are all {-lev['scaled_db']:.1f} dB lower than the errors alone imply, so that the "
                  "loudest one fits in 64)")
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
        res["channels"][n]["onsets"] = {
            "ref": len(vo), "mod": len(mo), "matched": len(devs), "unmatched": missing,
            "median_ms": statistics.median(devs) if devs else None,
            "worst_ms": max(devs, key=abs) if devs else None,
        }
    print()

    # ---- noise ----
    if "NOISE" in names:
        vo = onsets(vgm["NOISE"], thresh_db=-50)
        mo = onsets(mod["NOISE"], thresh_db=-50)
        print(f"NOISE: {len(vo)} ref hits, {len(mo)} MOD hits")
        noise_res: dict = {"ref_hits": len(vo), "mod_hits": len(mo)}
        res["noise"] = noise_res
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
            bv, bm = band_profile(sv, bands, 13000), band_profile(sm, bands, 13000)
            print("     VGM " + ' '.join(f"{x:>7.1f}" for x in bv))
            print("     MOD " + ' '.join(f"{x:>7.1f}" for x in bm))
            noise_res["bands_hz"] = [list(b) for b in bands]
            noise_res["band_db"] = {"vgm": bv, "mod": bm}
            print("  (a MOD profile that falls off above 4 kHz while VGM is flat means the LFSR"
                  " clock (tone2_n / synth_root) is too low)")
        print()

    # ---- DAC ----
    if "DAC" in names:
        vo = onsets(vgm["DAC"], thresh_db=-40, hold=0.08)
        mo = onsets(mod["DAC"], thresh_db=-40, hold=0.08)
        print(f"DAC: {len(vo)} ref hits, {len(mo)} MOD hits (first {min(6, len(vo))} shown)")
        res["dac"] = {"ref_hits": len(vo), "mod_hits": len(mo), "hits": []}
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
            res["dac"]["hits"].append({"t_s": t, "lowpeak_vgm_hz": float(pv), "lowpeak_mod_hz": float(pm)})
            print(f"  {k:3d} {t:>7.3f} {pv:>11.1f} {pm:>11.1f}   "
                  + ' '.join(f"{x:.0f}" for x in band_profile(sv, bands)))
            print(f"  {'':3} {'':7} {'':11} {'':11}   "
                  + ' '.join(f"{x:.0f}" for x in band_profile(sm, bands)))
        print("  (lowpeak differing by more than ~3% means the DAC sample plays at the wrong rate)")
    return res


def evaluate_checks(res: dict, balance_db: float | None, pitch_cents: float | None,
                    unmatched: int | None) -> list[dict]:
    """Apply the --fail-* thresholds to a report() result.  One entry per enabled threshold."""
    checks: list[dict] = []

    def add(name: str, limit: float, offenders: list[tuple[str, float]]) -> None:
        worst = max(offenders, key=lambda o: abs(o[1]), default=None)
        checks.append({
            "name": name, "limit": limit, "passed": not offenders,
            "offenders": [{"where": w, "value": v} for w, v in offenders],
            "worst": {"where": worst[0], "value": worst[1]} if worst else None,
        })

    if balance_db is not None:
        add("balance_db", balance_db,
            [(n, c["balance_db"]["diff"]) for n, c in res["channels"].items()
             if "balance_db" in c and abs(c["balance_db"]["diff"]) > balance_db])
    if pitch_cents is not None:
        # A note that is silent or has no measurable peak in the MOD is a pitch failure too.
        add("pitch_cents", pitch_cents,
            [(f"{n['channel']} {n['note']} @ {n['t_s']:.3f}s", n["mod_cents"]) for n in res["notes"]
             if n["silent_in_mod"] or math.isnan(n["mod_cents"]) or abs(n["mod_cents"]) > pitch_cents])
    if unmatched is not None:
        add("unmatched_onsets", unmatched,
            [(n, c["onsets"]["unmatched"]) for n, c in res["channels"].items()
             if "onsets" in c and c["onsets"]["unmatched"] > unmatched])
    return checks


def _json_safe(x):
    """NaN/inf are not valid JSON; numpy scalars are not serialisable."""
    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    if isinstance(x, (np.floating, np.integer)):
        x = x.item()
    if isinstance(x, float):
        return round(x, 4) if math.isfinite(x) else None
    return x


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="song YAML config (channel mapping, output_file)")
    ap.add_argument("vgz", help="reference VGM/VGZ recording of the same song")
    ap.add_argument("--mod", help="MOD to compare (default: config output_file)")
    ap.add_argument("--vgmplay", help="VGMPlay directory (default: VGMPLAY_DIR env var, then reference/vgz/vgmplay)")
    ap.add_argument("--workdir", help="where rendered WAVs go (default: output/compare/<config name>/)")
    ap.add_argument("--offset", type=float, help="MOD-minus-VGM time offset in seconds (default: auto)")
    ap.add_argument("--skip-render", action="store_true", help="reuse WAVs already in the workdir")
    ap.add_argument("--reuse-vgm", action="store_true",
                    help="re-render only the MOD; keep the reference WAVs in the workdir if they are all there")
    ap.add_argument("--ref", default="FM2", help="reference channel for balance table (default FM2)")
    ap.add_argument("--core", default="NUKE", help="VGMPlay YM2612 core: NUKE (default), GPGX, GENS")
    ap.add_argument("--max-rows", type=int, default=400, help="per-note rows to print (flagged rows always print)")
    ap.add_argument("--json", metavar="FILE", help="also write the results (and threshold checks) as JSON")
    ap.add_argument("--write-volumes", action="store_true",
                    help="set the config's sample_list volumes to the suggested ones (errors of 1 dB or more); "
                         "re-convert and re-run to verify")
    ap.add_argument("--fail-balance-db", type=float, metavar="DB",
                    help="exit 1 if any channel's balance vs --ref differs from the VGM by more than DB")
    ap.add_argument("--fail-pitch-cents", type=float, metavar="CENTS",
                    help="exit 1 if any note is more than CENTS off, silent or unmeasurable in the MOD")
    ap.add_argument("--fail-unmatched", type=int, metavar="N",
                    help="exit 1 if any channel has more than N reference onsets without a MOD onset")
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
        if args.reuse_vgm and all((workdir / f"vgm_{n}.wav").exists() for n in ["FULL", *vgm_names]):
            print("Reusing reference renders in the workdir")
        else:
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
    res = report(cfg, vgz, mod_path, workdir, args.offset, args.ref, args.max_rows)

    if args.write_volumes:
        changes = write_volumes(Path(args.config), res["instrument_levels"]["instruments"])
        print(f"sample_list volumes written to {args.config}:" if changes else "sample_list volumes: nothing to change")
        for line in changes:
            print(line)
        print()

    checks = evaluate_checks(res, args.fail_balance_db, args.fail_pitch_cents, args.fail_unmatched)
    passed = all(c["passed"] for c in checks)
    if checks:
        print()
        print("Threshold checks")
        for c in checks:
            if c["passed"]:
                print(f"  PASS  {c['name']} <= {c['limit']:g}")
            else:
                w = c["worst"]
                print(f"  FAIL  {c['name']} <= {c['limit']:g}: {len(c['offenders'])} over, "
                      f"worst {w['where']} = {w['value']:+.1f}")
    if args.json:
        out = {"config": args.config, "mod": str(mod_path), "vgz": str(vgz), **res,
               "checks": checks, "passed": passed}
        jp = Path(args.json)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(_json_safe(out), indent=2) + "\n", encoding="utf-8")
        print()
        print(f"JSON   : {jp}")
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
