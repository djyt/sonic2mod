"""Frame loop — drives SfxDriver against both chips and produces a mixed stream.

Both chips share one timeline at the YM2612's native rate (53267 Hz).  The SN76489 runs at
`psg_oversample` x that and is resampled down once at the end: at 1x its core's anti-aliasing is
a box average, and a rising square wave's aliases fold back under it (Jump at 8287 Hz: -25 dB of
the signal, heard as crackle).  The SN76489's output rate is fixed at SN76489_Init, which is why
the caller must construct it at NATIVE_RATE x psg_oversample.
"""

from __future__ import annotations

from array import array

from core.audio import DEFAULT_TAPS, resample, sounding_end
from core.chips.ym2612 import OPN2
from core.smps import ChannelType

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

# The PSG against the FM, each at its own full scale: VGMPlay (the reference the converter's volumes
# are measured against) plays a full PSG tone 10.2 dB under a full FM carrier with its Nuked core
# (12.0 dB with GPGX / GENS).  Measured 2026-10-14 on pure tones, and on Sonic 1's Ring (FM5) and
# Jump (PSG1) register writes: at 1.0 Jump came out 10 dB louder against the Ring than in VGMPlay,
# where the two are about equal, as on a Mega Drive's sound test
PSG_GAIN = 0.31

# All Sonic 1 SFX voices use release rate $0F (fastest) except SndC9 - Hidden
# Bonus, which has one operator at $08, so a short tail is plenty.
DEFAULT_TAIL_SECS = 1.0
DEFAULT_MAX_SECS = 10.0

# Below this fraction of full scale the tail is considered finished.
_SILENCE_FLOOR = 1.0 / 32768.0


class RenderResult:
    """Mixed stereo output at NATIVE_RATE, in normalised float units."""

    __slots__ = ("left", "rate", "right", "ticks", "truncated", "warnings")

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
        return max(abs(v) for side in (self.left, self.right) for v in side)


def render_sfx(song, opn2, sn, *, fps: float = 60.0,
               tail_secs: float = DEFAULT_TAIL_SECS,
               max_secs: float = DEFAULT_MAX_SECS,
               psg_gain: float = PSG_GAIN,
               psg_oob: str = "extend",
               native_rate: int = NATIVE_RATE,
               psg_oversample: int = 1,
               taps: int = DEFAULT_TAPS) -> RenderResult:
    """Play one parsed SFX and return its mixed stereo output.

    Args:
        song:      SmpsSong parsed from an SFX .asm.
        opn2, sn:  chip instances; both are reset here.
        fps:       V-int rate — 60 for NTSC, 50 for PAL.
        tail_secs: how long to keep rendering after the last track stops.
        max_secs:  hard cap, a safety net only (the longest Sonic 1 SFX is 4.32 s).
        psg_gain:  relative weight of the PSG against the FM after each chip is
                   scaled by its own full scale (PSG_GAIN: VGMPlay's balance).  1.0
                   would make one full-scale PSG channel match one full-scale FM one.
        psg_oob:   "extend" or "clamp" for PSG note indices past the driver table.
        native_rate: the YM2612's output rate at its clock (core.chips.ym2612.output_rate).
        psg_oversample: `sn` runs at native_rate x this (built so), resampled down at the end with
                   `taps` (settings.yaml psg_synthesis.oversample, samples.resample_taps).
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

    # Each chip's stream on its own: the PSG's raw, at its oversampled rate until the end.  An
    # effect with no PSG track leaves the chip silent: not rendered
    left: list[float] = []
    right: list[float] = []
    psg_left = array('i')
    psg_right = array('i')
    uses_psg = any(c.header.channel_type == ChannelType.PSG for c in song.channels)

    samples_per_frame = native_rate / fps
    max_samples = int(max_secs * native_rate)
    tail_samples = int(tail_secs * native_rate)

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
        boundary = round(frame * samples_per_frame)
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
        for fl, fr in fm_block:
            left.append(fl / FM_FULL_SCALE)
            right.append(fr / FM_FULL_SCALE)
        if uses_psg:
            pl, pr = sn.render_channels(n * psg_oversample)
            psg_left.extend(pl)
            psg_right.extend(pr)

        rendered = boundary

        if not alive:
            # This frame's mix, the PSG taken every psg_oversample-th sample: enough to hear silence
            block_peak = 0.0
            for i in range(len(left) - n, len(left)):
                p = i * psg_oversample
                pl, pr = (psg_gain * psg_left[p] / PSG_FULL_SCALE,
                          psg_gain * psg_right[p] / PSG_FULL_SCALE) if uses_psg else (0.0, 0.0)
                block_peak = max(block_peak, abs(left[i] + pl), abs(right[i] + pr))
            tail_remaining -= n
            if block_peak < _SILENCE_FLOOR or tail_remaining <= 0:
                break

        if rendered >= max_samples:
            truncated = True
            break

    opn2.end_capture()
    _mix_psg(left, psg_left, psg_gain, native_rate, psg_oversample, taps)
    _mix_psg(right, psg_right, psg_gain, native_rate, psg_oversample, taps)
    _trim_trailing_silence(left, right)
    return RenderResult(left, right, native_rate, ticks, list(driver.warnings), truncated)


def _mix_psg(fm: list[float], psg: array, gain: float, native_rate: int, oversample: int, taps: int) -> None:
    """Add the PSG's raw stream to the FM's, in place: resampled down to it once, then scaled to
    the FM's full scale x `gain`."""
    samples = resample(list(psg), native_rate * oversample, native_rate, taps=taps) if oversample > 1 else psg
    for i, p in enumerate(samples):
        fm[i] += gain * p / PSG_FULL_SCALE


def _trim_trailing_silence(left: list[float], right: list[float]) -> None:
    """Drop the trailing run of samples below the 16-bit noise floor, in place."""
    end = max(sounding_end(left, _SILENCE_FLOOR), sounding_end(right, _SILENCE_FLOOR))
    del left[end:]
    del right[end:]
