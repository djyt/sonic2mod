#!/usr/bin/env python3
"""Do two MODs sound the same?  Renders both with libopenmpt (ffmpeg) and compares the audio.

For a change meant to be inaudible — a sample trimmed where nothing hears it — the bytes
differ by design, so a byte diff proves nothing.  This renders each MOD channel of both files
and reports, per channel, the worst 20 ms window: how far the difference sits below the
signal there (dB).  A trim past what any note plays leaves no window above the floor.

    python tools/mod_render_diff.py before.mod after.mod
    python tools/mod_render_diff.py before.mod after.mod --floor -60   # report windows above -60 dB

Rebuilt samples carry fresh dither (core.pcm seeds it with the sample's length), so a sample
that only got shorter still differs by about -37 dB everywhere; hold the seed fixed in both
builds to see past it.

Needs numpy and an ffmpeg with the libopenmpt demuxer (as tools/vgm_compare.py).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from tools.vgm_compare import _MOD_FORMAT_CHANNELS, _isolate_mod

SR = 44100
WINDOW_SECS = 0.02
DEFAULT_FLOOR_DB = -50.0     # a difference this far below the loudest window is not reported
SILENCE = 1e-9


def _render(data: bytes, channel: int | None, tmp: Path, name: str) -> np.ndarray:
    """One channel of a MOD (None: all) as mono float samples."""
    mod = tmp / f"{name}.mod"
    raw = tmp / f"{name}.f32"
    mod.write_bytes(_isolate_mod(data, channel))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "libopenmpt",
                    "-sample_rate", str(SR), "-i", str(mod), "-ac", "1", "-f", "f32le", str(raw)], check=True)
    return np.fromfile(raw, dtype="<f4")


def _windows(x: np.ndarray) -> np.ndarray:
    """RMS of each WINDOW_SECS window."""
    n = int(SR * WINDOW_SECS)
    k = len(x) // n
    return np.sqrt(np.mean(x[:k * n].reshape(k, n) ** 2, axis=1)) if k else np.zeros(0)


def compare(a: np.ndarray, b: np.ndarray) -> tuple[float, float, int]:
    """(worst window's difference against the loudest window, dB; its time, s; windows compared)."""
    n = max(len(a), len(b))
    a = np.pad(a, (0, n - len(a)))
    b = np.pad(b, (0, n - len(b)))
    diff = _windows(a - b)
    ref = max(float(_windows(a).max(initial=0.0)), SILENCE)
    if not len(diff):
        return -np.inf, 0.0, 0
    worst = int(np.argmax(diff))
    return 20 * np.log10(max(float(diff[worst]), SILENCE) / ref), worst * WINDOW_SECS, len(diff)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--floor", type=float, default=DEFAULT_FLOOR_DB,
                    help=f"dB below the loudest window a difference may reach unreported (default {DEFAULT_FLOOR_DB:g})")
    args = ap.parse_args()
    da, db = Path(args.a).read_bytes(), Path(args.b).read_bytes()
    nch = _MOD_FORMAT_CHANNELS.get(da[1080:1084].decode("ascii", "replace"), 4)
    if nch != _MOD_FORMAT_CHANNELS.get(db[1080:1084].decode("ascii", "replace"), 4):
        raise SystemExit("the two MODs have different channel counts")

    worst_all = -np.inf
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for ch in [None, *range(nch)]:
            label = "mix" if ch is None else f"channel {ch + 1}"
            dbv, at, _n = compare(_render(da, ch, tmp, "a"), _render(db, ch, tmp, "b"))
            worst_all = max(worst_all, dbv)
            mark = "  <-- differs" if dbv > args.floor else ""
            shown = "identical" if dbv == -np.inf or dbv < -150 else f"{dbv:6.1f} dB at {at:6.2f} s"
            print(f"{label:>10}: worst 20 ms difference {shown}{mark}")
    sys.exit(1 if worst_all > args.floor else 0)


if __name__ == "__main__":
    main()
