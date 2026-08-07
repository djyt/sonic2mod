"""Frame loop — drives SfxDriver against both chips and produces a mixed stream.

Both chips are clocked at the YM2612's native rate (53267 Hz) so they share one
timeline and the mix needs no resampling until the very end.  The SN76489's
output rate is fixed at SN76489_Init, which is why the caller must construct it
at NATIVE_RATE.
"""

from __future__ import annotations

from ym2612.wrapper import OPN2

from .chips import psg_silence_all
from .driver import SfxDriver

NATIVE_RATE = OPN2.NATIVE_RATE          # 53267

# Measured full-scale output of a single channel at maximum volume, used to put
# the two chips on a common scale before mixing.  Verified empirically against
# the DLLs: one YM2612 channel with TL=0 peaks at +/-789 (identical for 1-carrier
# and 4-carrier algorithms — the per-channel ladder clamps), one SN76489 tone
# channel at attenuation 0 peaks at +/-4096, and its noise channel at +/-2048.
FM_FULL_SCALE = 789.0
PSG_FULL_SCALE = 4096.0

# All Sonic 1 SFX voices use release rate $0F (fastest) except SndC9 - Hidden
# Bonus, which has one operator at $08, so a short tail is plenty.
DEFAULT_TAIL_SECS = 1.0
DEFAULT_MAX_SECS = 10.0

# Below this fraction of full scale the tail is considered finished.
_SILENCE_FLOOR = 1.0 / 32768.0


class RenderResult:
    """Mixed stereo output at NATIVE_RATE, in normalised float units."""

    __slots__ = ("left", "right", "rate", "ticks", "warnings", "truncated")

    def __init__(self, left, right, rate, ticks, warnings, truncated):
        self.left = left
        self.right = right
        self.rate = rate
        self.ticks = ticks
        self.warnings = warnings
        self.truncated = truncated

    def __len__(self) -> int:
        return len(self.left)

    @property
    def seconds(self) -> float:
        return len(self.left) / self.rate if self.rate else 0.0

    @property
    def peak(self) -> float:
        if not self.left:
            return 0.0
        return max(max(abs(v) for v in self.left), max(abs(v) for v in self.right))


def render_sfx(song, opn2, sn, *, fps: float = 60.0,
               tail_secs: float = DEFAULT_TAIL_SECS,
               max_secs: float = DEFAULT_MAX_SECS,
               psg_gain: float = 1.0,
               psg_oob: str = "extend") -> RenderResult:
    """Play one parsed SFX and return its mixed stereo output.

    Args:
        song:      SmpsSong parsed from an SFX .asm.
        opn2, sn:  chip instances; both are reset here.
        fps:       V-int rate — 60 for NTSC, 50 for PAL.
        tail_secs: how long to keep rendering after the last track stops.
        max_secs:  hard cap, a safety net only (the longest Sonic 1 SFX is 4.32 s).
        psg_gain:  relative weight of the PSG against the FM after each chip is
                   scaled by its own full scale.  1.0 means one full-scale PSG
                   channel matches one full-scale FM channel.
        psg_oob:   "extend" or "clamp" for PSG note indices past the driver table.
    """
    opn2.reset("ym2612")
    sn.reset()

    # Pre-timeline silence, mirroring PSGSilenceAll / the FM key-offs the driver
    # performs before an SFX starts.  Done before capture is enabled so these
    # writes do not consume timeline.
    psg_silence_all(sn)
    for channel in (2, 3, 4):
        opn2.key_off(channel)

    driver = SfxDriver(song, opn2, sn, psg_oob=psg_oob)
    opn2.begin_capture()

    left: list[float] = []
    right: list[float] = []

    samples_per_frame = NATIVE_RATE / fps
    max_samples = int(max_secs * NATIVE_RATE)
    tail_samples = int(tail_secs * NATIVE_RATE)

    rendered = 0
    frame = 0
    ticks = 0
    alive = True
    tail_remaining = tail_samples
    truncated = False

    while True:
        if alive:
            alive = driver.tick()
            ticks += 1

        frame += 1
        boundary = int(round(frame * samples_per_frame))
        n = boundary - rendered

        # Register writes during this frame already advanced the chip; their audio
        # was captured rather than discarded, so render only the remainder.
        captured = opn2.take_capture()
        if len(captured) > n:
            # Would need a frame with >440 register writes; impossible in this data.
            driver.warnings.append(
                f"frame {frame}: {len(captured)} captured samples exceed frame length {n}"
            )
            captured = captured[:n]
        fm_block = captured + opn2.render_samples(n - len(captured))
        psg_block = sn.render_samples(n)

        for (fl, fr), (pl, pr) in zip(fm_block, psg_block, strict=True):
            left.append(fl / FM_FULL_SCALE + psg_gain * pl / PSG_FULL_SCALE)
            right.append(fr / FM_FULL_SCALE + psg_gain * pr / PSG_FULL_SCALE)

        rendered = boundary

        if not alive:
            block_peak = 0.0
            for i in range(len(left) - n, len(left)):
                block_peak = max(block_peak, abs(left[i]), abs(right[i]))
            tail_remaining -= n
            if block_peak < _SILENCE_FLOOR or tail_remaining <= 0:
                break

        if rendered >= max_samples:
            truncated = True
            break

    opn2.end_capture()
    _trim_trailing_silence(left, right)
    return RenderResult(left, right, NATIVE_RATE, ticks, list(driver.warnings), truncated)


def _trim_trailing_silence(left: list[float], right: list[float]) -> None:
    """Drop the trailing run of samples below the 16-bit noise floor, in place."""
    end = len(left)
    while end > 0 and abs(left[end - 1]) < _SILENCE_FLOOR and abs(right[end - 1]) < _SILENCE_FLOOR:
        end -= 1
    del left[end:]
    del right[end:]
