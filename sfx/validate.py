"""Standalone checks for the offline SFX driver.

Usage::

    python sfx/validate.py [--sfx-dir sonic_1/sfx]

Verifies the driver tables against known-good values from the assembled ROM, the
resampler against an analytic sine, and — if the SFX sources are present — parse
lengths, stereo panning and non-silence for a diagnostic subset.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.smps_parser import SmpsParser          # noqa: E402
from sfx import tables                           # noqa: E402
from sfx.batch import render_one                 # noqa: E402
from sfx.render import NATIVE_RATE               # noqa: E402
from sfx.resample import resample                # noqa: E402
from sn76489.wrapper import SN76489              # noqa: E402
from ym2612.wrapper import OPN2                  # noqa: E402

# Channel tick totals, derived independently from the assembly byte stream.
EXPECTED_TICKS = {
    "SndCD - Switch": [2],
    "SndA3 - Death": [54],
    "SndA8 - SS Goal": [114],
    "SndB0 - Saw": [181],
    "SndB5 - Ring": [37],
    "SndBF - Get Continue": [252, 259, 252],
    "SndB9 - Collapse": [146, 144, 145, 120],
}

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}] {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def check_tables() -> None:
    print("Driver tables")
    check("FM table length 96", len(tables.FM_FREQUENCIES) == 96)
    check("PSG table length 70", len(tables.PSG_FREQUENCIES) == 70)
    for i, want in ((0, 0x025E), (1, 0x0284), (2, 0x02AB), (11, 0x047C)):
        got = tables.FM_FREQUENCIES[i]
        check(f"FM[{i}] == ${want:04X}", got == want, f"got ${got:04X}")
    for i, want in ((0, 0x0356), (1, 0x0326), (11, 0x01C4)):
        got = tables.PSG_FREQUENCIES[i]
        check(f"PSG[{i}] == ${want:04X}", got == want, f"got ${got:04X}")
    check("nMaxPSG entry is the degenerate N=1", tables.PSG_FREQUENCIES[69] == 1)
    check("PSG extension leaves the real table intact",
          tables.PSG_FREQUENCIES_EXTENDED[:70] == tables.PSG_FREQUENCIES)
    check("PSG7 envelope has 6 leading zeros",
          tables.PSG_ENVELOPES[6][:7] == (0, 0, 0, 0, 0, 0, 1),
          "configs/settings.yaml has only 5 — the driver is authoritative")


def check_resampler() -> None:
    print("\nResampler")
    fs, ft, f0 = NATIVE_RATE, 44100, 1000.0
    n = int(0.25 * fs)
    src = [math.sin(2 * math.pi * f0 * i / fs) for i in range(n)]
    out = resample(src, fs, ft)
    ref = [math.sin(2 * math.pi * f0 * i / ft) for i in range(len(out))]
    span = range(200, len(out) - 200)
    err = max(abs(out[i] - ref[i]) for i in span)
    db = 20 * math.log10(err) if err else -999
    check("1 kHz sine reconstructs below -60 dB error", db < -60.0, f"{db:.1f} dB")
    check("output length matches the rate ratio",
          abs(len(out) - n * ft / fs) <= 1, f"{len(out)} samples")


def check_parse(sfx_dir: Path) -> None:
    print("\nParse lengths")
    for stem, expected in EXPECTED_TICKS.items():
        path = sfx_dir / f"{stem}.asm"
        if not path.is_file():
            check(stem, False, "file not found")
            continue
        song = SmpsParser().parse_file(str(path))
        got = [sum(e.note.duration for e in ch.events if e.is_note) for ch in song.channels]
        check(f"{stem} ticks {expected}", got == expected, f"got {got}")


def check_render(sfx_dir: Path) -> None:
    print("\nRender")
    opn2 = OPN2(mode="ym2612")
    sn = SN76489(clock_rate=3_579_545, sample_rate=NATIVE_RATE)
    try:
        pans = {}
        for stem in ("SndB5 - Ring", "SndCE - Ring Left Speaker", "SndAE - Fireball"):
            path = sfx_dir / f"{stem}.asm"
            if not path.is_file():
                check(stem, False, "file not found")
                continue
            r = render_one(str(path), opn2, sn)
            energy_l = sum(abs(v) for v in r.left)
            energy_r = sum(abs(v) for v in r.right)
            check(f"{r.name} is not silent", r.peak > 0.0, f"peak {r.peak:.3f}")
            pans[r.name] = 20 * math.log10((energy_l + 1e-9) / (energy_r + 1e-9))

        if "B5_Ring" in pans:
            check("B5_Ring is panned right", pans["B5_Ring"] < -6.0,
                  f"{pans['B5_Ring']:+.1f} dB L/R")
        if "CE_Ring_Left_Speaker" in pans:
            check("CE_Ring_Left_Speaker is panned left", pans["CE_Ring_Left_Speaker"] > 6.0,
                  f"{pans['CE_Ring_Left_Speaker']:+.1f} dB L/R")
    finally:
        sn.shutdown()


def check_amiga() -> None:
    print("\n8-bit Amiga export")
    from sfx.amiga import (
        PAL_CLOCK,
        candidate_rates,
        dc_block,
        normalise,
        pad_for_paula,
        quantise_8bit,
    )

    grid = candidate_rates()
    by_note = {n: (r, p) for r, p, n in grid}
    check("C-2 maps to period 428", by_note["C-2"][1] == 428,
          f"{by_note['C-2'][0]:.0f} Hz")
    check("C-3 maps to period 214", by_note["C-3"][1] == 214,
          f"{by_note['C-3'][0]:.0f} Hz")
    check("every candidate rate is PAL_CLOCK/period",
          all(abs(r - PAL_CLOCK / p) < 1e-6 for r, p, _ in grid))

    # DC blocker must remove a constant offset without disturbing leading silence
    signal = [0.0] * 100 + [0.5] * 4000
    blocked = dc_block(signal, NATIVE_RATE, 30.0)
    check("DC blocker removes a constant offset", abs(blocked[-1]) < 1e-3,
          f"settles to {blocked[-1]:.5f}")
    check("DC blocker leaves leading silence at zero",
          all(abs(v) < 1e-12 for v in blocked[:100]))

    # Quantisation: full-scale input must reach the rails and stay in range
    ramp = [(i / 500.0) * 2.0 - 1.0 for i in range(1000)]
    scaled, peak = normalise(ramp)
    check("normalise scales to unity peak", abs(max(abs(v) for v in scaled) - 1.0) < 1e-12,
          f"original peak {peak:.3f}")
    data = quantise_8bit(scaled, shape=1, dither=True)
    signed = [b - 256 if b > 127 else b for b in data]
    check("quantised samples stay in signed 8-bit range",
          all(-128 <= v <= 127 for v in signed))
    check("quantised peak reaches the rails", max(abs(v) for v in signed) >= 126,
          f"peak {max(abs(v) for v in signed)}")

    check("quantisation is deterministic",
          quantise_8bit(scaled) == quantise_8bit(scaled))

    padded, offset, length = pad_for_paula(b'\x01\x02\x03')
    check("padding yields an even length", len(padded) % 2 == 0, f"{len(padded)} bytes")
    check("repeat points at a silent trailing word",
          length == 2 and padded[offset:offset + 2] == b'\x00\x00')


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the offline SFX driver")
    parser.add_argument('--sfx-dir', default=str(_HERE.parent / "sonic_1" / "sfx"))
    args = parser.parse_args()

    check_tables()
    check_resampler()
    check_amiga()

    sfx_dir = Path(args.sfx_dir)
    if sfx_dir.is_dir():
        check_parse(sfx_dir)
        check_render(sfx_dir)
    else:
        print(f"\n  (skipping parse/render checks — {sfx_dir} not present)")

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): " + ", ".join(_failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
