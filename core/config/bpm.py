"""The MOD BPM a song's SMPS tempo header plays at."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BpmReport:
    """How far a whole-number MOD BPM is from the driver's tempo (convert.py's report)."""
    exact: float                 # the BPM the driver's tempo works out at
    error_pct: float             # the MOD's BPM against it
    better: dict | None          # the best bpm_rounding_options() row closer than that, if any


def bpm_report(bpm: int, tempo_divider, tempo_modifier, ticks_per_row, speed, fps: float = 60.0) -> BpmReport | None:
    """How far `bpm` (derive_bpm's) is from the exact tempo, and which target_speed would leave it
    closer; None when the tempo cannot be derived."""
    exact = exact_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps)
    if math.isnan(exact):
        return None
    err = (bpm / exact - 1) * 100
    better = [o for o in bpm_rounding_options(tempo_divider, tempo_modifier, ticks_per_row, fps)
              if abs(o["error_pct"]) < abs(err) - 0.05]
    return BpmReport(exact, err, better[0] if better else None)


def derive_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps: float = 60.0):
    """Derive MOD BPM from SMPS tempo parameters.

    The SMPS driver runs once a frame. A tempo counter decrements each frame;
    when it hits 0, TempoWait fires — resetting the counter to `modifier`
    and adding +1 to all tracks' DurationTimeout (cancelling that frame's
    decrement). Effective tick rate = fps * (modifier - 1) / modifier.

    Note durations from assembly are multiplied by `divider`.

    MOD timing: rows_per_second = (BPM / 2.5) / speed

    Setting SMPS rows/sec equal to MOD rows/sec:
        BPM = fps * (modifier - 1) * speed * 2.5 / (modifier * divider * ticks_per_row)

    Args:
        tempo_divider: SMPS header divider (multiplies note durations)
        tempo_modifier: SMPS header modifier (TempoWait fires every N frames)
        ticks_per_row: SMPS ticks per MOD row
        speed: MOD speed (ticks per row in ProTracker)
        fps: the driver's frames a second (core.smps.update_hz: 60 NTSC V-int)

    Returns:
        Integer BPM clamped to 32–255
    """
    if tempo_modifier <= 1 or tempo_divider < 1:
        return 150  # fallback
    bpm = fps * (tempo_modifier - 1) * speed * 2.5 / (tempo_modifier * tempo_divider * ticks_per_row)
    return max(32, min(255, round(bpm)))


def exact_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps: float = 60.0) -> float:
    """derive_bpm() before rounding and clamping (nan when the tempo cannot be derived)."""
    if tempo_modifier <= 1 or tempo_divider < 1:
        return float("nan")
    return fps * (tempo_modifier - 1) * speed * 2.5 / (tempo_modifier * tempo_divider * ticks_per_row)


def bpm_rounding_options(tempo_divider, tempo_modifier, ticks_per_row, fps: float = 60.0,
                         speeds=(2, 3, 4, 5, 6, 7, 8)) -> list[dict]:
    """How far the whole-number MOD BPM is from the driver's tempo, for each candidate speed.

    A MOD BPM is an integer, so a song whose exact BPM is 98.44 (Special Stage: modifier 8,
    divider 2, 2 ticks per row, speed 3) runs 0.44 % slow at 98 - 139 ms behind the hardware
    over its 33 s pass.  `target_speed` only changes how many MOD ticks a row has, not the row
    grid, so it can be chosen to make the BPM (nearly) whole: speed 6 gives 196.875 -> 197,
    0.06 %.  Returns [{speed, exact, bpm, error_pct}] for the speeds whose BPM fits 32-255,
    best first (ties keep the smaller speed).
    """
    out = []
    for speed in speeds:
        exact = exact_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps)
        if exact != exact or not 32 <= exact <= 255:
            continue
        bpm = round(exact)
        out.append({"speed": speed, "exact": exact, "bpm": bpm, "error_pct": (bpm / exact - 1) * 100})
    out.sort(key=lambda o: (abs(o["error_pct"]), o["speed"]))
    return out
