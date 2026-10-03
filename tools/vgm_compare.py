#!/usr/bin/env python3
"""Rendered per-channel comparison of a converted MOD against its VGM/VGZ reference.

Renders the reference VGZ one chip channel at a time (VGMPlay with mute masks)
and the converted MOD one MOD channel at a time (ffmpeg + libopenmpt on
channel-isolated copies), then lines the two up and reports, per channel:

  * every note: reference pitch, pitch of both renders in cents from it (measured on the
    note's strongest partial), level in both renders and the level difference
  * the pitch verdict: tools/vgm_pitch_audit.py's symbolic audit (chip frequency registers vs
    the pitch each MOD note sounds at) - the authority on "is every note right"
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
    python tools/vgm_compare.py cfg.yaml ref.vgz --merged          # the merged build: each MOD channel
                                                                   # against the sum of its source channels
    python tools/vgm_compare.py cfg.yaml ref.vgz --offset 0.25     # force MOD-minus-VGM offset (s)

    # CI-style: machine-readable results + non-zero exit when a threshold is exceeded
    python tools/vgm_compare.py cfg.yaml ref.vgz --json output/compare/title.json --fail-balance-db 2

Renders land in --workdir (default: output/compare/<config-name>/).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("ERROR: numpy is required (pip install numpy)", file=sys.stderr)
    sys.exit(1)

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio import pitch_name
from core.audit import (
    LEVEL_MAX_ERR,
    LEVEL_MAX_SPREAD,
    SR,
    VGM_CHANNELS,
    VIB_MIN_NOTE,
    OnsetMatch,
    audio_onsets,
    audit_pitches,
    audit_settings,
    band_profile,
    db,
    envelope_offset,
    find_vgmplay,
    group_masks,
    harmonic_cents,
    instrument_levels,
    keyon_onsets,
    load_wav,
    mod_note_events,
    mod_pitch_timeline,
    note_start_offset,
    onset_match,
    onsets,
    prepare_audit,
    render_mod_channels,
    render_vgm_channels,
    rms,
    seg_at,
    spectrum,
    vibrato_estimates,
    write_volumes,
)
from core.config import ConversionConfig
from core.merge import column_sources, prepare_merged_config
from core.mod import ModImage, read_mod, timed_pass
from core.plan import load_config
from core.ui import print_audit
from core.vgm import DAC_NAME, NoteStart, VgmLog, note_starts, pitch_segments, read_vgm

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(x: float, w: int = 7, p: int = 1) -> str:
    return f"{x:>{w}.{p}f}" if not math.isnan(x) else f"{'nan':>{w}}"


def _hz(f: int) -> str:
    return f"{f // 1000}k" if f >= 1000 and f % 1000 == 0 else str(f)


@dataclass(frozen=True)
class _Reference:
    """The recording, read once: its log and its note starts on every chip."""

    log: VgmLog
    notes: list[NoteStart]

    @classmethod
    def read(cls, vgz: Path) -> _Reference:
        log = read_vgm(vgz)
        return cls(log, note_starts(log))


def _chip_channel(source: str, noise_used: bool) -> str:
    """The chip channel a source renders as: PSG3 in noise mode is the chip's NOISE channel."""
    return "NOISE" if (source == "PSG3" and noise_used) else source


def _say_alignment(offset: float, how: str) -> None:
    print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms ({how})")


@dataclass
class _Renders:
    """Both renders per channel name: stereo for every level figure (see load_wav), mono mixes for the rest."""

    vgm_st: dict[str, np.ndarray]
    mod_st: dict[str, np.ndarray]
    vgm: dict[str, np.ndarray] = field(init=False)
    mod: dict[str, np.ndarray] = field(init=False)

    def __post_init__(self) -> None:
        self.vgm = {n: a.mean(axis=1) for n, a in self.vgm_st.items()}
        self.mod = {n: a.mean(axis=1) for n, a in self.mod_st.items()}


def _align(offset: float | None, chip_tl: dict, mod_tl: dict, sources: dict[str, int], rd: _Renders) -> float:
    """The MOD-minus-VGM offset (s), given or found; prints which."""
    if offset is not None:
        _say_alignment(offset, "given")
        return offset

    # Note starts from the register log against the MOD's note rows: exact, and immune to the
    # envelope method's failure on sparse or tempo-drifting songs (Chaos Emerald +920 ms,
    # Drowning +1205 ms).  The envelope correlation is kept for songs with no pitched notes.
    if not any(mod_tl.values()):
        offset = envelope_offset(rd.vgm["FULL"], rd.mod["FULL"])
        _say_alignment(offset, "auto, envelope cross-correlation")
        return offset
    offset = note_start_offset(chip_tl, mod_tl, sources)
    env = envelope_offset(rd.vgm["FULL"], rd.mod["FULL"])
    _say_alignment(offset, "auto, note starts"
                   + (f"; envelope correlation says {env * 1000:+.0f} ms — ignored" if abs(env - offset) > 0.03 else ""))
    return offset


def _note_result(ch: str, evs: list[tuple], i: int, rd: _Renders, offset: float) -> dict:
    """Key-on `evs[i]` measured in both renders: pitch on its strongest partial, level."""
    t, fref, note = evs[i].ms / 1000.0, evs[i].hz, pitch_name(evs[i].hz)
    t_end = evs[i + 1].ms / 1000.0 if i + 1 < len(evs) else t + 2.0
    win = min(0.3 if fref < 200 else 0.1, max(0.04, t_end - t - 0.03))
    harm, cv = harmonic_cents(seg_at(rd.vgm[ch], t + 0.025, win), fref)
    _, cm = harmonic_cents(seg_at(rd.mod[ch], t + offset + 0.025, win), fref, harm)
    lv = db(rms(seg_at(rd.vgm_st[ch], t + 0.025, win)))
    lm = db(rms(seg_at(rd.mod_st[ch], t + offset + 0.025, win)))
    # MOD against VGM, not against the key-on register value: on a grace note or a legato
    # run the window holds the NEXT pitch in both renders, and that is not an error.
    return {
        "channel": ch, "t_s": t, "note": note, "ref_hz": fref, "vgm_cents": cv, "mod_cents": cm,
        "pitch_diff_cents": cm - cv,
        "vgm_db": lv, "mod_db": lm, "diff_db": lm - lv, "silent_in_mod": lm < -70,
    }


def _note_flag(n: dict) -> str:
    if n["silent_in_mod"]:
        return "  <-- SILENT in MOD"
    cv, cm = n["vgm_cents"], n["mod_cents"]
    return "  <-- PITCH" if (not math.isnan(cv) and (math.isnan(cm) or abs(n["pitch_diff_cents"]) > 25)) else ""


def _report_notes(per_ch: dict[str, list], rd: _Renders, offset: float, max_rows: int, res: dict,
                  width: int, intro: tuple[str, str], summary: str) -> None:
    """Per-note pitch and level at every key-on of `per_ch`, then a summary per channel.

    `width`: the channel column's; `intro`: the table's two title lines; `summary`: the summary's.
    """
    if not per_ch:
        return
    for line in intro:
        print(line)
    print(f"{'chan':<{width}}{'t_vgm':>7}  {'ref':<4}{'ref_hz':>8}  {'vgm_c':>6}  {'mod_c':>6}  {'vgm_dB':>7}  "
          f"{'mod_dB':>7}  {'diff':>6}")
    print("-" * (72 + width))

    # Every note; the first max_rows and every flagged one printed
    level_diff: dict[str, list[float]] = {}
    cent_err: dict[str, list[float]] = {}
    printed = 0
    for ch, evs in per_ch.items():
        for i in range(len(evs)):
            n = _note_result(ch, evs, i, rd, offset)
            res["notes"].append(n)
            level_diff.setdefault(ch, []).append(n["diff_db"])
            if not math.isnan(n["mod_cents"]):
                cent_err.setdefault(ch, []).append(n["mod_cents"])
            flag = _note_flag(n)
            if max_rows > 0 and printed >= max_rows and not flag:
                continue
            print(f"{ch:<{width}}{n['t_s']:>7.3f}  {n['note']:<4}{n['ref_hz']:>8.1f}  {_fmt(n['vgm_cents'], 6)}  "
                  f"{_fmt(n['mod_cents'], 6)}  {n['vgm_db']:>7.1f}  {n['mod_db']:>7.1f}  {n['diff_db']:>6.1f}{flag}")
            printed += 1

    # Per channel
    print()
    print(summary)
    print(f"{'chan':<{width}}{'notes':>6}  {'pitch err cents (median/min/max)':<34}  {'level diff dB (median)':<22}")
    for ch in per_ch:
        ce = cent_err.get(ch, [float('nan')])
        ld = level_diff.get(ch, [float('nan')])
        print(f"{ch:<{width}}{len(per_ch[ch]):>6}  {statistics.median(ce):>8.1f} / {min(ce):>6.1f} / {max(ce):>6.1f}"
              f"{'':<8}  {statistics.median(ld):>+8.1f}")
        res["channels"][ch].update({
            "notes": len(per_ch[ch]),
            "pitch_cents": {"median": statistics.median(ce), "min": min(ce), "max": max(ce)},
            "level_diff_db_median": statistics.median(ld),
        })
    print()


def _report_pitch_verdict(chip_tl: dict, chip_end: float, mod_tl: dict, mod_end: float, sources: dict[str, int],
                          offset: float, pitch_tol: float, res: dict) -> None:
    """The symbolic verdict (core.audit), not the audio windows of the per-note table."""
    if not any(mod_tl.values()):
        return
    pa = audit_pitches(chip_tl, chip_end, mod_tl, mod_end, sources, offset, tolerance=pitch_tol)
    res["pitch_audit"] = pa
    n_ok = sum(c["ok"] for c in pa["channels"].values())
    print(f"Pitch verdict (chip frequency registers vs the pitch each MOD note sounds at; wrong = over {pitch_tol:g} cents)")
    print(f"  {n_ok} of {n_ok + pa['bad']} notes right" + ("" if pa["bad"] else " - every note is at the hardware's pitch"))
    print_audit(pa, 60.0, indent="  ")
    if pa["bad"]:
        print("  (times and every wrong note: python tools/vgm_pitch_audit.py <config> <vgz> --list)")
    print()


def _vibrato_flag(vv: dict | None, vm: dict | None) -> str:
    """What is wrong with the MOD's vibrato (or beating) against the VGM's; both are not None-None."""
    if vv is None or vm is None:
        return "  <-- MISSING in MOD" if vm is None else "  <-- not in VGM"
    if "beat" in (vv["kind"], vm["kind"]):
        return "  <-- BEAT RATE" if abs(vm["rate_hz"] / vv["rate_hz"] - 1) > 0.15 else ""
    if (abs(vm["rate_hz"] / vv["rate_hz"] - 1) > 0.15
            or abs(vm["depth_cents"] - vv["depth_cents"]) > max(5.0, 0.3 * vv["depth_cents"])):
        return "  <-- VIBRATO"
    return ""


def _vibrato_cell(v: dict | None) -> str:
    if not v:
        return f"{'none':>18}"
    return f"{v['rate_hz']:>5.2f} Hz +/-{v['depth_cents']:>4.1f} c{'b' if v['kind'] == 'beat' else ' '}"


def _report_vibrato(per_ch: dict[str, list], rd: _Renders, offset: float, res: dict) -> None:
    """Vibrato on long notes, in both renders.

    Key-off is not in the event list, so a note runs to the next key-on on its channel and
    pitch_track() drops the frames where it has already faded.
    """
    if not per_ch:
        return
    # The long notes, then both renders' estimates of every one at once
    long: list[tuple[str, float, str, float]] = []          # (channel, t, note, duration)
    for ch, evs in per_ch.items():
        for i, r in enumerate(evs):
            t = r.ms / 1000.0
            dur = (evs[i + 1].ms / 1000.0 - t) if i + 1 < len(evs) else 3.0
            if dur >= VIB_MIN_NOTE:
                long.append((ch, t, pitch_name(r.hz), dur))
    segs = []
    for ch, t, _note, dur in long:
        span = min(dur, 4.0) - 0.02
        segs += [seg_at(rd.vgm[ch], t + 0.01, span), seg_at(rd.mod[ch], t + offset + 0.01, span)]
    estimates = vibrato_estimates(segs)

    long_notes = len(long)
    vib_rows: list[str] = []
    for k, (ch, t, note, dur) in enumerate(long):
        vv, vm = estimates[2 * k], estimates[2 * k + 1]
        if vv is None and vm is None:
            continue
        flag = _vibrato_flag(vv, vm)
        vib_rows.append(f"{ch:<6}{t:>7.3f}  {note:<4}{dur:>6.2f}   {_vibrato_cell(vv)}   {_vibrato_cell(vm)}{flag}")
        res["vibrato"].append({"channel": ch, "t_s": t, "note": note, "dur_s": dur,
                               "vgm": vv, "mod": vm, "mismatch": bool(flag)})

    print(f"Vibrato ({long_notes} notes of {VIB_MIN_NOTE} s or longer checked; rows = notes that modulate in either render)")
    if not vib_rows:
        print("  none found")
        print()
        return
    print(f"{'chan':<6}{'t_vgm':>7}  {'ref':<4}{'dur':>6}   {'VGM rate / depth':>18}   {'MOD rate / depth':>18}")
    print("-" * 68)
    for row in vib_rows:
        print(row)
    print("  (4xy: rate = x*(speed-1)*BPM/(160*speed) Hz; depth grows with y and with the note's period)")
    print("  (b = beating of detuned FM carriers, not smpsModSet: the level swings at the same rate."
          "  Its rate follows")
    print("   sample playback speed, so a BEAT RATE mismatch points at synth_root / multi-sampling,"
          " not at 4xy)")
    print()


def _report_balance(names: list[str], ref: str, rd: _Renders, res: dict, width: int, title: str) -> None:
    """Whole-song level of every channel relative to `ref`, in both renders, and the two mixes'."""
    print(title)
    rv_ref, rm_ref = db(rms(rd.vgm_st[ref])), db(rms(rd.mod_st[ref]))
    for n in names:
        rv, rm = db(rms(rd.vgm_st[n])) - rv_ref, db(rms(rd.mod_st[n])) - rm_ref
        note = "" if abs(rm - rv) < 2 else "   <-- rebalance"
        print(f"  {n:<{width}} {rv:>8.1f} {rm:>8.1f} {rm - rv:>+8.1f}{note}")
        res["channels"][n]["balance_db"] = {"vgm": rv, "mod": rm, "diff": rm - rv}
    res["ref_channel"] = ref

    vgm_full, mod_full = rd.vgm_st["FULL"], rd.mod_st["FULL"]
    res["mix"] = {"vgm_rms_db": db(rms(vgm_full)), "vgm_peak": float(np.abs(vgm_full).max()),
                  "mod_rms_db": db(rms(mod_full)), "mod_peak": float(np.abs(mod_full).max())}
    print(f"  (absolute: VGM mix {db(rms(vgm_full)):.1f} dBFS peak {np.abs(vgm_full).max():.2f};"
          f" MOD mix {db(rms(mod_full)):.1f} dBFS peak {np.abs(mod_full).max():.2f})")
    print()


def _note_times(per_ch: dict[str, list], names: list[str], notes: list[NoteStart], rd: _Renders) -> dict[str, list[float]]:
    """Reference note starts (s) per channel: key-ons, the DAC's detected audio onsets."""
    note_times = {ch: [n.ms / 1000.0 for n in evs] for ch, evs in per_ch.items()}
    if "NOISE" in names:
        note_times["NOISE"] = [n.ms / 1000.0 for n in notes if n.channel == "NOISE"]
    if "DAC" in names:
        note_times["DAC"] = onsets(rd.vgm["DAC"], thresh_db=-40, hold=0.08)
    return note_times


def _mod_events(cfg: ConversionConfig, mod: ModImage) -> tuple[dict[int, list[tuple]], dict[int, tuple[str, int]]]:
    """mod_note_events, a detune variant (core.plan.detune) played as its base: its sample a few cents
    off, at its volume."""
    events_by_chan, samples = mod_note_events(mod, cfg.target_speed)
    if cfg.detune_plan is not None:
        base_of = cfg.detune_plan.base_of
        events_by_chan = {c: [(t, base_of(ins), cxx) for t, ins, cxx in evs] for c, evs in events_by_chan.items()}
    return events_by_chan, samples


def _print_instrument_levels(lev: dict) -> None:
    if not lev["instruments"]:
        return
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
        flag = "  <-- channels disagree" if it["spread_db"] > LEVEL_MAX_SPREAD else ""
        if abs(it["err_db"]) > LEVEL_MAX_ERR:
            flag = "  <-- too far off to be a volume problem"
        print(f"{it['instrument']:>4} {it['name']:<22} {it['volume']:>3} {it['notes']:>5} {it['err_db']:>+6.1f} "
              f"{it['spread_db']:>6.1f} {sug:>7}   {detail}{flag}")
    if lev["scaled_db"] < -0.05:
        print(f"  (suggestions are all {-lev['scaled_db']:.1f} dB lower than the errors alone imply, so that the "
              "loudest one fits in 64)")
    print()


def _print_onsets(name: str, m: OnsetMatch, width: int) -> None:
    if not m.devs:
        print(f"  {name:<{width}} {m.ref:3d} ref onsets, {m.mod:3d} MOD onsets; none matched")
    else:
        print(f"  {name:<{width}} {m.ref:3d} ref onsets, {m.mod:3d} MOD onsets; matched {len(m.devs)}: "
              f"median {statistics.median(m.devs):+.0f} ms, worst {max(m.devs, key=abs):+.0f} ms; "
              f"unmatched {m.missing}" + (f", MOD-only {m.extra}" if m.extra else "")
              + (f", drift {m.drift_ms:+.0f} ms" if m.drift_ms is not None and abs(m.drift_ms) >= 20 else ""))
    if m.lost:
        print(f"         no MOD note row at: {', '.join(f'{t:.2f}' for t in m.lost[:12])}"
              + (f" ... (+{len(m.lost) - 12})" if len(m.lost) > 12 else "") + " s")


def _report_onsets(names: list[str], note_times: dict[str, list[float]], events_by_chan: dict[int, list[tuple]],
                   chan_of: dict[str, int], offset: float, mod_end: float, rd: _Renders, res: dict) -> None:
    """Onset timing per channel.

    Channels with key-on events: the chip's key-ons against the MOD's note rows, one to one.  An
    audio onset detector cannot do this on sustained channels - a re-keyed note over a ringing
    one raises no envelope edge, and a MOD re-trigger where the hardware ties adds one (GHZ read
    28-143 unmatched per channel with every note in place).  The DAC has no key-on (its rows are
    PCM seeks), so it keeps the detector, the same one on both sides so slow attacks cancel.
    """
    print("Onset timing (chip key-on -> MOD note row, one to one within 40 ms of the running deviation;"
          " DAC: detected audio onsets)")
    for n in names:
        symbolic = n in note_times and n != "DAC"
        if symbolic:
            m = keyon_onsets([t for t in note_times[n] if t + offset < mod_end - 0.15],
                              [e[0] - offset for e in events_by_chan.get(chan_of[n], [])])
        else:
            m = audio_onsets(rd.vgm[n], rd.mod[n], -50 if n == "NOISE" else -40, offset)
        _print_onsets(n, m, 6)
        res["channels"][n]["onsets"] = {
            "method": "key-on" if symbolic else "audio", "mod_only": m.extra, "drift_ms": m.drift_ms,
            "unmatched_s": m.lost,
            "ref": m.ref, "mod": m.mod, "matched": len(m.devs), "unmatched": m.missing,
            "median_ms": statistics.median(m.devs) if m.devs else None,
            "worst_ms": max(m.devs, key=abs) if m.devs else None,
        }
    print()


def _report_noise(rd: _Renders, offset: float, res: dict) -> None:
    """NOISE hits: decay envelope of a few, and the spectrum of one (the LFSR rate)."""
    vgm_n, mod_n = rd.vgm["NOISE"], rd.mod["NOISE"]
    vo = onsets(vgm_n, thresh_db=-50)
    mo = onsets(mod_n, thresh_db=-50)
    print(f"NOISE: {len(vo)} ref hits, {len(mo)} MOD hits")
    noise_res: dict = {"ref_hits": len(vo), "mod_hits": len(mo)}
    res["noise"] = noise_res

    # Decay of the first, second and last hit
    steps = [0.0, 0.017, 0.033, 0.05, 0.067, 0.083, 0.1, 0.133, 0.167, 0.2, 0.25]
    print("  decay envelope (dB at ms after onset): " + ' '.join(f"{int(s * 1000):>5d}" for s in steps))
    for k in sorted({0, 1, len(vo) - 1} & set(range(len(vo)))):
        ev = [db(rms(seg_at(vgm_n, vo[k] + s, 0.02))) for s in steps]
        em = [db(rms(seg_at(mod_n, vo[k] + offset + s, 0.02))) for s in steps]
        print(f"  hit {k:2d} VGM  " + ' '.join(f"{x:>5.0f}" for x in ev))
        print("         MOD  " + ' '.join(f"{x:>5.0f}" for x in em))

    # Spectrum of the first hit that sounds in BOTH renders: the MOD can lack the very first one
    # (Star Light), and an all-silent window says nothing about the LFSR rate.
    bands = [(0, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 13000)]
    both = [t for t in vo if db(rms(seg_at(mod_n, t + offset + 0.005, 0.04))) > -70]
    if not both:
        print()
        return
    sv = seg_at(vgm_n, both[0] + 0.005, 0.04)
    sm = seg_at(mod_n, both[0] + offset + 0.005, 0.04)
    print(f"  band energy dB rel total <13 kHz (hit at {both[0]:.3f} s): "
          + ' '.join(f"{_hz(a)}-{_hz(b)}" for a, b in bands))
    bv, bm = band_profile(sv, bands, 13000), band_profile(sm, bands, 13000)
    print("     VGM " + ' '.join(f"{x:>7.1f}" for x in bv))
    print("     MOD " + ' '.join(f"{x:>7.1f}" for x in bm))
    noise_res["bands_hz"] = [list(b) for b in bands]
    noise_res["band_db"] = {"vgm": bv, "mod": bm}
    print("  (a MOD profile that falls off above 4 kHz while VGM is flat means the LFSR"
          " clock (tone2_n / synth_root) is too low)")
    print()


def _report_dac(rd: _Renders, offset: float, res: dict) -> None:
    """The first DAC hits: low-frequency peak (the playback rate) and band profile."""
    vgm_d, mod_d = rd.vgm["DAC"], rd.mod["DAC"]
    vo = onsets(vgm_d, thresh_db=-40, hold=0.08)
    mo = onsets(mod_d, thresh_db=-40, hold=0.08)
    print(f"DAC: {len(vo)} ref hits, {len(mo)} MOD hits (first {min(6, len(vo))} shown)")
    res["dac"] = {"ref_hits": len(vo), "mod_hits": len(mo), "hits": []}
    bands = [(0, 150), (150, 300), (300, 600), (600, 1200), (1200, 2400), (2400, 4800), (4800, 9600), (9600, 22050)]
    print("  hit   t_vgm  lowpeak_vgm lowpeak_mod   band dB VGM / MOD: " + ' '.join(f"{_hz(a)}-{_hz(b)}" for a, b in bands))
    for k in range(min(6, len(vo))):
        t = vo[k]
        sv = seg_at(vgm_d, t + 0.005, 0.08)
        sm = seg_at(mod_d, t + offset + 0.005, 0.08)
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


def report(cfg: ConversionConfig, song, recording: _Reference, mod_path: Path, workdir: Path,
           offset: float | None, ref_chan: str, max_rows: int, pitch_tol: float = 35.0) -> dict:
    """Print the comparison and return the same numbers as a JSON-serialisable dict.

    `pitch_tol`: cents before the symbolic pitch audit counts a note as wrong.
    """
    log, notes = recording.log, recording.notes
    noise_used = any(n.channel == "NOISE" for n in notes)
    chan_map = {c.source: c.mod_channel for c in cfg.channels if c.source in VGM_CHANNELS or c.source == "PSG3"}
    names = [_chip_channel(src, noise_used) for src in chan_map]
    chan_of = {n: chan_map[src] for n, src in zip(names, chan_map, strict=True)}
    sources = {c.source: c.mod_channel for c in cfg.channels}

    # Renders: the MOD's are named after the source, the VGM's after the chip channel
    vgm_st = {n: load_wav(workdir / f"vgm_{n}.wav", stereo=True) for n in ["FULL", *names]}
    mod_st = {n: load_wav(workdir / f"mod_{src}.wav", stereo=True) for src, n in zip(chan_map, names, strict=True)}
    mod_st["FULL"] = load_wav(workdir / "mod_FULL.wav", stereo=True)
    rd = _Renders(vgm_st, mod_st)

    offset_auto = offset is None
    chip_tl, chip_end = pitch_segments(log)
    mod = read_mod(mod_path)
    mod_tl, mod_end = mod_pitch_timeline(mod, cfg, song)
    offset = _align(offset, chip_tl, mod_tl, sources, rd)
    print()
    res: dict = {
        "offset_ms": offset * 1000, "offset_auto": offset_auto,
        "channels": {n: {} for n in names}, "notes": [], "vibrato": [],
    }

    # Pitched notes (FM + PSG tone).  Notes the recording plays as the MOD's single pass ends or later
    # (its second time round the loop) have nothing to be compared with; 150 ms keeps the measuring
    # window inside the render.
    per_ch: dict[str, list] = {}
    for n in notes:
        if (n.channel in rd.vgm and n.channel not in (DAC_NAME, "NOISE") and n.hz > 0
                and n.ms / 1000.0 + offset < mod_end - 0.15):
            per_ch.setdefault(n.channel, []).append(n)
    _report_notes(per_ch, rd, offset, max_rows, res, 6,
                  ("Per-note comparison (levels are dBFS of the isolated channel; diff = MOD - VGM; vgm_c / mod_c = cents",
                   "from the chip's key-on pitch, measured in the audio - PITCH flags the two renders disagreeing)"),
                  "Per-channel summary")
    _report_pitch_verdict(chip_tl, chip_end, mod_tl, mod_end, sources, offset, pitch_tol, res)
    _report_vibrato(per_ch, rd, offset, res)

    ref = ref_chan if ref_chan in rd.vgm else names[0]
    _report_balance(names, ref, rd, res, 6,
                    f"Whole-song channel RMS relative to {ref} (dB, L/R power):   VGM     MOD    MOD-VGM")

    # Per-instrument levels and onsets, against the MOD's notes as one pass plays them
    note_times = _note_times(per_ch, names, notes, rd)
    events_by_chan, samples = _mod_events(cfg, mod)
    lev = instrument_levels(note_times, {n: chan_of[n] for n in note_times}, events_by_chan, samples,
                            mod_end, rd.vgm_st, rd.mod_st, offset)
    res["instrument_levels"] = lev
    _print_instrument_levels(lev)
    _report_onsets(names, note_times, events_by_chan, chan_of, offset, mod_end, rd, res)

    if "NOISE" in names:
        _report_noise(rd, offset, res)
    if "DAC" in names:
        _report_dac(rd, offset, res)
    return res


def report_merged(recording: _Reference, mod_path: Path, workdir: Path, offset: float | None, ref_chan: str,
                  labels: dict[str, list[str]], max_rows: int = 400) -> dict:
    """The merged build: every MOD channel against the sum of the chip channels folded onto it.

    Whole-song balance, audio onsets, and the pitch of each merged channel at its PRIMARY's
    chip key-ons (the first source of the label): both renders hold the primary and its
    followers, so the same partial is measured on both sides and the difference is the MOD's
    error.  The symbolic verdict (one note stream per channel) does not apply.
    """
    names = list(labels)
    notes = recording.notes
    rd = _Renders({n: load_wav(workdir / f"vgm_{n}.wav", stereo=True) for n in ["FULL", *names]},
                  {n: load_wav(workdir / f"mod_{n}.wav", stereo=True) for n in ["FULL", *names]})
    offset_auto = offset is None
    if offset is None:
        # Within three quarters of a second: a repetitive song correlates a whole bar off
        offset = envelope_offset(rd.vgm["FULL"], rd.mod["FULL"], max_lag=0.75)
        _say_alignment(offset, "auto, envelope cross-correlation")
    else:
        _say_alignment(offset, "given")
    print()
    res: dict = {"offset_ms": offset * 1000, "offset_auto": offset_auto, "merged": True,
                 "channels": {n: {"sources": labels[n]} for n in names}, "notes": [], "vibrato": []}
    mod_end = len(rd.mod["FULL"]) / SR

    # Per-note pitch and level at the primary's key-ons
    per_ch: dict[str, list] = {}
    for n in names:
        primary = labels[n][0]
        if primary in ("DAC", "NOISE"):
            continue
        per_ch[n] = [s for s in notes if s.channel == primary and s.hz > 0 and s.ms / 1000.0 + offset < mod_end - 0.15]
    _report_notes(per_ch, rd, offset, max_rows, res, 14,
                  ("Per-note comparison at the primary's key-ons (levels are dBFS of the merged channel; diff = MOD - VGM;",
                   "vgm_c / mod_c = cents from the chip's key-on pitch, measured in the audio - PITCH flags the two renders"
                   " disagreeing)"),
                  "Per-channel summary (at the primary's key-ons)")

    ref = next((n for n in names if ref_chan in labels[n]), names[0])
    _report_balance(names, ref, rd, res, 14,
                    f"Whole-song channel RMS relative to {ref} (dB, L/R power); a merged channel against the sum of its"
                    " chip channels:   VGM     MOD    MOD-VGM")

    print("Onset timing (audio onsets on both sides, matched within 40 ms; a merged channel's onsets are"
          " every note-on of its sources)")
    for n in names:
        m = audio_onsets(rd.vgm[n], rd.mod[n], -50 if "NOISE" in labels[n] else -40, offset)
        _print_onsets(n, m, 14)
        res["channels"][n]["onsets"] = {
            "method": "audio", "ref": m.ref, "mod": m.mod, "matched": len(m.devs), "unmatched": m.missing,
            "median_ms": statistics.median(m.devs) if m.devs else None,
            "worst_ms": max(m.devs, key=abs) if m.devs else None,
        }
    print()
    return res


_BLOCK_FLAG_DB = 2.0     # a column's block this far from the song's anchor is flagged
_KEYON_SECS = 0.1        # the attack window a key-on's level is measured over


def mod_pattern_spans(mod: ModImage, speed: int = 6) -> list[tuple[int, float, float]]:
    """[(pattern, start s, end s)] in play order on one pass (Bxx / Dxx followed, stopping at
    the song loop), timed as mod_note_events times its notes."""
    spans: list[tuple[int, float, float]] = []
    rows, end = timed_pass(mod, speed)
    for i, r in enumerate(rows):
        if not spans or spans[-1][0] != r.pattern or r.row == 0:
            spans.append((r.pattern, r.start, r.start))
        spans[-1] = (r.pattern, spans[-1][1], rows[i + 1].start if i + 1 < len(rows) else end)
    return spans


def _pattern_label(patterns: list[int]) -> str:
    """Hex pattern numbers as runs: [1, 2, 3, 4, 13] -> '1-4 d'."""
    runs: list[list[int]] = []
    for q in patterns:
        if runs and q == runs[-1][-1] + 1:
            runs[-1].append(q)
        else:
            runs.append([q])
    return " ".join(f"{r[0]:x}" if len(r) == 1 else f"{r[0]:x}-{r[-1]:x}" for r in runs)


def _column_blocks(layout: dict[int, dict[int, list[str]]], spans: list[tuple[int, float, float]]) -> list[dict]:
    """Each column's runs of consecutive patterns with the same sources."""
    blocks: list[dict] = []
    for c in sorted(layout):
        for p, t0, t1 in spans:
            srcs = layout[c].get(p, [])
            last = blocks[-1] if blocks and blocks[-1]["column"] == c else None
            if last is not None and last["sources"] == srcs and abs(last["t1"] - t0) < 1e-6:
                last["patterns"].append(p)
                last["t1"] = t1
                continue
            blocks.append({"column": c, "sources": srcs, "patterns": [p], "t0": t0, "t1": t1})
    return blocks


def report_merged_patterns(cfg: ConversionConfig, recording: _Reference, mod_path: Path, workdir: Path,
                           offset: float | None, chip_names: dict[str, str]) -> dict:
    """The merged build of a merge_patterns: config, each MOD column against the hardware.

    A column's sources change per pattern (core.merge.column_sources), so its reference is
    built from one render per chip channel: in each pattern, the sum of the channels whose
    notes sound on that column.  Levels are relative to the song's anchor (the median block),
    which cancels the two renders' overall gain.  "key-ons" is the median over the primary's
    key-ons of its attack: a fold that sums its layers too loud shows there first.

        col  patterns   sources          block  key-ons
          2  1-4        FM5+FM3+FM4+PSG1  +0.3     +2.1   <-- level

    `chip_names`: {source: the chip channel its render is named after} (PSG3 -> NOISE).
    Pooled notes (merge_fill, fill: true) sound on whatever column is silent: in no reference.
    """
    spans = mod_pattern_spans(read_mod(mod_path))
    layout = column_sources(cfg, sorted({p for p, _, _ in spans}))
    columns = sorted(layout)
    vgm_st = {n: load_wav(workdir / f"vgm_{n}.wav", stereo=True) for n in ["FULL", *set(chip_names.values())]}
    mod_st = {n: load_wav(workdir / f"mod_{n}.wav", stereo=True) for n in ["FULL", *(f"col{c}" for c in columns)]}
    offset_auto = offset is None
    if offset is None:
        offset = envelope_offset(vgm_st["FULL"].mean(axis=1), mod_st["FULL"].mean(axis=1), max_lag=0.75)
        print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (auto, envelope cross-correlation)")
    else:
        print(f"Alignment: MOD lags VGM by {offset * 1000:+.0f} ms (given)")
    print()
    lag = round(offset * SR)

    # Each column's reference, in MOD time: per pattern, the sum of its sources' chip renders
    ref_col: dict[int, np.ndarray] = {}
    for c in columns:
        out = np.zeros_like(mod_st[f"col{c}"])
        for p, t0, t1 in spans:
            a, b = int(t0 * SR), min(int(t1 * SR), len(out))
            for src in layout[c].get(p, []):
                v = vgm_st[chip_names[src]]
                lo, hi = max(a - lag, 0), min(b - lag, len(v))
                if hi > lo:
                    out[lo + lag:hi + lag] += v[lo:hi]
        ref_col[c] = out

    # Every block's level, whole and at its primary's key-ons
    notes = recording.notes
    blocks = _column_blocks(layout, spans)
    for blk in blocks:
        c, a, b = blk["column"], int(blk["t0"] * SR), int(blk["t1"] * SR)
        mod_c = mod_st[f"col{c}"]
        blk["vgm_db"], blk["mod_db"] = db(rms(ref_col[c][a:b])), db(rms(mod_c[a:b]))
        primary = chip_names.get(blk["sources"][0]) if blk["sources"] else None
        diffs = []
        for n in notes:
            t = n.ms / 1000.0 + offset
            if n.channel != primary or not blk["t0"] <= t < blk["t1"] - _KEYON_SECS:
                continue
            lv, lm = db(rms(seg_at(ref_col[c], t, _KEYON_SECS))), db(rms(seg_at(mod_c, t, _KEYON_SECS)))
            if lv > -70 and lm > -70:
                diffs.append(lm - lv)
        blk["keyons"] = len(diffs)
        blk["keyon_raw_db"] = statistics.median(diffs) if diffs else float("nan")

    audible = [blk["mod_db"] - blk["vgm_db"] for blk in blocks if blk["vgm_db"] > -70 and blk["mod_db"] > -70]
    anchor = statistics.median(audible) if audible else 0.0
    print("Per column and pattern block, against the sum of its chip channels there (dB, L/R power;")
    print(f"MOD - VGM relative to the song's anchor {anchor:+.1f} dB; key-ons = {_KEYON_SECS * 1000:.0f} ms of the"
          " primary's attacks)")
    print(f"{'col':>4}  {'patterns':<10} {'sources':<24} {'VGM':>7} {'MOD':>7} {'block':>7} {'key-ons':>8} {'n':>4}")
    for blk in blocks:
        silent = blk["vgm_db"] <= -70 and blk["mod_db"] <= -70
        blk["diff_db"] = float("nan") if silent else blk["mod_db"] - blk["vgm_db"] - anchor
        blk["keyon_db"] = blk.pop("keyon_raw_db") - anchor
        flag = "  <-- level" if any(abs(x) >= _BLOCK_FLAG_DB for x in (blk["diff_db"], blk["keyon_db"])
                                    if not math.isnan(x)) else ""
        print(f"{blk['column'] + 1:>4}  {_pattern_label(blk['patterns']):<10} {'+'.join(blk['sources']) or '-':<24} "
              f"{blk['vgm_db']:>7.1f} {blk['mod_db']:>7.1f} {_fmt(blk['diff_db'], 7)} {_fmt(blk['keyon_db'], 8)} "
              f"{blk['keyons']:>4}{flag}")
    print()

    res: dict = {"offset_ms": offset * 1000, "offset_auto": offset_auto, "merged": True, "per_pattern": True,
                 "anchor_db": anchor, "notes": [], "channels": {},
                 "blocks": [{**{k: v for k, v in blk.items() if k not in ("t0", "t1")}, "t_s": [blk["t0"], blk["t1"]]}
                            for blk in blocks]}

    # Whole song per column, and its audio onsets against the reference's
    print("Per column, whole song (dB relative to the anchor) and audio onsets (matched within 40 ms)")
    for c in columns:
        name = f"col{c}"
        rv, rm = db(rms(ref_col[c])), db(rms(mod_st[name]))
        thr = -50 if any("PSG3" in v for v in layout[c].values()) else -40
        devs, missing = onset_match(onsets(ref_col[c].mean(axis=1), thresh_db=thr),
                                     onsets(mod_st[name].mean(axis=1), thresh_db=thr))
        diff = rm - rv - anchor
        flag = "" if abs(diff) < _BLOCK_FLAG_DB else "  <-- rebalance"
        med = f"median {statistics.median(devs):+.0f} ms" if devs else "none matched"
        print(f"  column {c + 1}  {diff:+6.1f} dB{flag:<16}  {len(devs) + missing:4d} ref onsets: {med}, "
              f"unmatched {missing}")
        res["channels"][f"column {c + 1}"] = {
            "balance_db": {"vgm": rv, "mod": rm, "diff": diff},
            "onsets": {"ref": len(devs) + missing, "matched": len(devs), "unmatched": missing,
                       "median_ms": statistics.median(devs) if devs else None},
        }
    pooled = sorted({g.primary for g in cfg.merge if g.fill} | set(cfg.merge_fill))
    if pooled:
        print(f"  (pooled notes of {', '.join(pooled)} sound on whichever column is silent: in no column's reference)")
    print()
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
    if pitch_cents is not None and "pitch_audit" in res:
        # The symbolic audit, run at this tolerance (report(pitch_tol=...)).
        bad = []
        for src, c in res["pitch_audit"]["channels"].items():
            bad += [(f"{src} {w['chip']} @ {w['t_s']:.3f}s plays {w['mod']}", w["cents"]) for w in c["wrong_notes"]]
            bad += [(f"{src} {m['chip']} @ {m['t_s']:.3f}s has no MOD note", float("nan")) for m in c["missing_notes"]]
        bad += [(f"{n['channel']} {n['note']} @ {n['t_s']:.3f}s is silent in the MOD", float("nan"))
                for n in res["notes"] if n["silent_in_mod"]]
        add("pitch_cents", pitch_cents, bad)
    elif pitch_cents is not None:
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
                    help="exit 1 if any MOD note is more than CENTS from the chip's frequency register (symbolic "
                         "audit, as vgm_pitch_audit.py), missing, or silent in the MOD render")
    ap.add_argument("--fail-unmatched", type=int, metavar="N",
                    help="exit 1 if any channel has more than N reference onsets without a MOD onset")
    ap.add_argument("--settings", metavar="PATH",
                    help="settings the MOD was converted with (default: settings.yaml beside the config, "
                         "else configs/settings.yaml): whether it has detune variants")
    ap.add_argument("--merged", action="store_true",
                    help="audit the merged build (convert.py --merged): each MOD channel against the sum of the "
                         "chip channels folded onto it (balance and onsets; no per-note audit)")
    args = ap.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    cfg = load_config(args.config)
    if args.merged:
        try:
            prepare_merged_config(cfg)          # followers off, channels packed, merge_output_file
        except ValueError as e:
            raise SystemExit(f"ERROR: {e}") from e
    # synth_root / synth_shift come from the song (what the converter does before rendering), and
    # so do the detune variants; without them the symbolic verdict reads every shifted entry as wrong
    song = prepare_audit(cfg, args.settings, args.config)
    mod_path = Path(args.mod or cfg.output_file)
    vgz = Path(args.vgz)
    for p in (mod_path, vgz):
        if not p.exists():
            raise SystemExit(f"ERROR: file not found: {p}")
    workdir = Path(args.workdir or Path("output") / "compare" / (Path(args.config).stem + ("_merged" if args.merged else "")))

    reference = _Reference.read(vgz)
    notes = reference.notes
    noise_used = any(n.channel == "NOISE" for n in notes)
    masks = None
    labels: dict[str, list[str]] = {}
    per_pattern = args.merged and bool(cfg.merge_patterns_named)
    chip_names: dict[str, str] = {}
    if per_pattern:
        # merge_patterns: a column's sources change per pattern - one render per chip channel,
        # summed per pattern by report_merged_patterns; one MOD render per output column
        chip_names = {c.source: _chip_channel(c.source, noise_used) for c in cfg.channels}
        chip_names = {s: n for s, n in chip_names.items() if n in VGM_CHANNELS}
        vgm_names = sorted(set(chip_names.values()))
        chan_map = {f"col{c}": c for c in sorted({c.mod_channel for c in cfg.channels if c.enabled})}
        if cfg.merge_drop:
            print(f"Dropped from the merged build (in the VGM mix, not the MOD): {', '.join(cfg.merge_drop)}")
    elif args.merged:
        # One render per live MOD channel, named after the chip channels folded onto it
        followers_of = {g.primary: list(g.followers) for g in cfg.merge}
        chan_map = {}
        for c in sorted((c for c in cfg.channels if c.enabled), key=lambda c: c.mod_channel):
            srcs = [_chip_channel(s, noise_used) for s in (c.source, *followers_of.get(c.source, []))]
            srcs = [s for s in srcs if s in VGM_CHANNELS]
            if srcs:
                labels["+".join(srcs)] = srcs
                chan_map["+".join(srcs)] = c.mod_channel
        masks = {lab: group_masks(srcs) for lab, srcs in labels.items()}
        vgm_names = list(labels)
        if cfg.merge_drop:
            print(f"Dropped from the merged build (in the VGM mix, not the MOD): {', '.join(cfg.merge_drop)}")
    else:
        chan_map = {c.source: c.mod_channel for c in cfg.channels}
        vgm_names = [_chip_channel(s, noise_used) for s in chan_map]
        vgm_names = [n for n in vgm_names if n in VGM_CHANNELS]

    if not args.skip_render:
        if args.reuse_vgm and all((workdir / f"vgm_{n}.wav").exists() for n in ["FULL", *vgm_names]):
            print("Reusing reference renders in the workdir")
        else:
            vgmplay = find_vgmplay(args.vgmplay)
            print(f"Rendering reference channels with {vgmplay} ...")
            synth, _psg = audit_settings(args.settings, args.config)
            render_vgm_channels(vgz, vgm_names, vgmplay, workdir, args.core, masks, cache_dir=synth.render_cache)
        print("Rendering MOD channels with ffmpeg/libopenmpt ...")
        render_mod_channels(mod_path, chan_map, workdir)
        print()

    print(f"Config : {args.config}")
    print(f"MOD    : {mod_path}")
    print(f"VGZ    : {vgz}")
    print(f"Renders: {workdir}")
    print()
    if per_pattern:
        res = report_merged_patterns(cfg, reference, mod_path, workdir, args.offset, chip_names)
    elif args.merged:
        res = report_merged(reference, mod_path, workdir, args.offset, args.ref, labels, args.max_rows)
    else:
        res = report(cfg, song, reference, mod_path, workdir, args.offset, args.ref, args.max_rows,
                     pitch_tol=args.fail_pitch_cents if args.fail_pitch_cents is not None else 35.0)

    if args.write_volumes and "instrument_levels" not in res:
        print("--write-volumes: not for the merged build (its instruments are measured in the reference build)")
    elif args.write_volumes:
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
