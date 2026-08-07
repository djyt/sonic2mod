"""Discovery, batch rendering and global normalisation."""

from __future__ import annotations

import glob
import math
import os
import re

from core.smps_parser import SmpsParser
from sn76489.wrapper import SN76489
from ym2612.wrapper import OPN2

from .amiga import (
    DEFAULT_MAX_RATE,
    PAL_CLOCK,
    choose_rate,
    dc_block,
    nearest_candidate,
    normalise,
    pad_for_paula,
    quantise_8bit,
)
from .render import NATIVE_RATE, render_sfx
from .resample import DEFAULT_TAPS, resample, resample_stereo
from .wav import write_wav

# Mega Drive SN76489 clock (NTSC).
PSG_CLOCK = 3_579_545

_NAME_RE = re.compile(r'^Snd([0-9A-Fa-f]{2})\s*(?:-\s*(.*))?$')


class SfxRender:
    """One rendered SFX, held in memory until the global peak is known."""

    __slots__ = ("path", "name", "left", "right", "rate", "ticks", "channels",
                 "warnings", "truncated", "peak")

    def __init__(self, path, name, left, right, rate, ticks, channels, warnings, truncated):
        self.path = path
        self.name = name
        self.left = left
        self.right = right
        self.rate = rate
        self.ticks = ticks
        self.channels = channels
        self.warnings = warnings
        self.truncated = truncated
        self.peak = max(
            max((abs(v) for v in left), default=0.0),
            max((abs(v) for v in right), default=0.0),
        )

    @property
    def seconds(self) -> float:
        return len(self.left) / self.rate if self.rate else 0.0


def discover(sfx_dir: str) -> list[str]:
    """Return the SFX .asm files in driver order.

    Raises FileNotFoundError with an actionable message when the directory is
    absent — `/sonic_1` is gitignored, so a fresh clone will not have it.
    """
    if not os.path.isdir(sfx_dir):
        raise FileNotFoundError(
            f"SFX directory not found: {sfx_dir}\n"
            "  The Sonic 1 disassembly is not bundled with this repo.\n"
            "  Point --sfx-dir at a directory of 'SndXX - Name.asm' files."
        )
    return sorted(glob.glob(os.path.join(sfx_dir, 'Snd*.asm')))


def output_name(path: str) -> str:
    """'SndB5 - Ring.asm' -> 'B5_Ring';  'SndA2.asm' -> 'A2'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    match = _NAME_RE.match(stem)
    if not match:
        return re.sub(r'[^A-Za-z0-9_]+', '_', stem).strip('_')
    ident, title = match.group(1).upper(), match.group(2)
    if not title:
        return ident
    return ident + '_' + re.sub(r'[^A-Za-z0-9_]+', '_', title.strip()).strip('_')


def render_one(path, opn2, sn, *, fps=60.0, tail_secs=1.0, max_secs=10.0,
               psg_gain=1.0, psg_oob="extend", target_rate=44100,
               taps=DEFAULT_TAPS) -> SfxRender:
    """Parse, render and resample a single SFX file."""
    song = SmpsParser().parse_file(path)
    warnings: list[str] = []

    if not song.header.is_sfx:
        warnings.append("no SFX header found — parsed as music, output may be wrong")
    if not song.channels:
        raise ValueError(f"{os.path.basename(path)}: no channels found")

    result = render_sfx(song, opn2, sn, fps=fps, tail_secs=tail_secs,
                        max_secs=max_secs, psg_gain=psg_gain, psg_oob=psg_oob)
    warnings.extend(result.warnings)
    if result.truncated:
        warnings.append(f"hit the {max_secs:g}s cap — output truncated")

    left, right = result.left, result.right
    rate = result.rate
    if target_rate and target_rate != rate:
        left, right = resample_stereo(left, right, rate, target_rate, taps=taps)
        rate = target_rate

    return SfxRender(
        path=path,
        name=output_name(path),
        left=left,
        right=right,
        rate=rate,
        ticks=result.ticks,
        channels=[(c.header.channel_type, c.header.hw_channel) for c in song.channels],
        warnings=warnings,
        truncated=result.truncated,
    )


def render_all(paths, *, fps=60.0, tail_secs=1.0, max_secs=10.0, psg_gain=1.0,
               psg_oob="extend", target_rate=44100, taps=DEFAULT_TAPS,
               progress=None) -> list[SfxRender]:
    """Render every path with a single shared pair of chip instances."""
    opn2 = OPN2(mode="ym2612")
    sn = SN76489(clock_rate=PSG_CLOCK, sample_rate=NATIVE_RATE)
    try:
        renders = []
        for path in paths:
            if progress is not None:
                progress(path)
            renders.append(render_one(
                path, opn2, sn, fps=fps, tail_secs=tail_secs, max_secs=max_secs,
                psg_gain=psg_gain, psg_oob=psg_oob, target_rate=target_rate, taps=taps,
            ))
        return renders
    finally:
        sn.shutdown()


def global_scale(renders, peak_dbfs: float = -0.3) -> float:
    """One scale factor for the whole set, preserving relative loudness.

    Normalising each file independently would flatten the composer's balance —
    the ring collect is meant to sit well below the death jingle.
    """
    peak = max((r.peak for r in renders), default=0.0)
    if peak <= 0.0:
        return 1.0
    return (32767.0 * (10.0 ** (peak_dbfs / 20.0))) / peak


def write_all(renders, out_dir: str, scale: float) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for render in renders:
        path = os.path.join(out_dir, render.name + '.wav')
        write_wav(path, render.left, render.right, render.rate, scale=scale)
        written.append(path)
    return written


def peak_dbfs(render, scale: float) -> float:
    """Post-scaling peak of one render, in dBFS."""
    value = render.peak * scale
    if value <= 0.0:
        return -math.inf
    return 20.0 * math.log10(min(value, 32767.0) / 32767.0)


# ---------------------------------------------------------------------------
# 8-bit Amiga export
# ---------------------------------------------------------------------------

class AmigaSample:
    """One effect prepared as an 8-bit Paula sample."""

    __slots__ = ("name", "data", "rate", "period", "note", "volume", "finetune",
                 "repeat_offset", "repeat_length", "level", "channels", "warnings")

    def __init__(self, name, data, rate, period, note, repeat_offset, repeat_length,
                 level, channels, warnings):
        self.name = name
        self.data = data
        self.rate = rate
        self.period = period
        self.note = note
        self.repeat_offset = repeat_offset
        self.repeat_length = repeat_length
        self.level = level            # pre-normalisation peak, for the volume column
        self.channels = channels
        self.warnings = warnings
        self.volume = 64
        self.finetune = 0

    @property
    def seconds(self) -> float:
        return len(self.data) / self.rate if self.rate else 0.0


def prepare_8bit(render, *, max_rate=DEFAULT_MAX_RATE, flat_rate=None,
                 shape=1, dither=True, taps=DEFAULT_TAPS,
                 clock=PAL_CLOCK, energy_frac=0.99) -> AmigaSample:
    """Mono-fold, DC-block, resample once, normalise, dither and quantise.

    `render` must be at the chip's native rate — resampling through 44.1 kHz
    first would add a second stage of error for nothing.
    """
    mono = [(lv + rv) * 0.5 for lv, rv in zip(render.left, render.right, strict=True)]
    mono = dc_block(mono, render.rate)

    if flat_rate is not None:
        rate, period, note = nearest_candidate(flat_rate, clock)
    else:
        rate, period, note = choose_rate(mono, render.rate, energy_frac=energy_frac,
                                         max_rate=max_rate, clock=clock)

    target = int(round(rate))
    resampled = resample(mono, render.rate, target, taps=taps) if target != render.rate else mono

    scaled, level = normalise(resampled)
    data = quantise_8bit(scaled, shape=shape, dither=dither)
    data, repeat_offset, repeat_length = pad_for_paula(data)

    return AmigaSample(
        name=render.name,
        data=data,
        rate=target,
        period=period,
        note=note,
        repeat_offset=repeat_offset,
        repeat_length=repeat_length,
        level=level,
        channels=render.channels,
        warnings=list(render.warnings),
    )


def assign_volumes(samples) -> None:
    """Set each sample's MOD volume so the set keeps its composed balance.

    Every sample is normalised to full scale, so the volume column is what
    restores relative loudness — and because Paula applies volume after the DAC,
    the resolution gained by normalising is not given back.

    Levels are measured AFTER DC removal and resampling, deliberately, so the
    balance is right for the audio that actually plays rather than for the 16-bit
    reference.  Two consequences worth knowing:

    * Effects with heavy DC (B8 and C8_Burning were both over 20% of peak) had
      that offset inflating their measured peak; once removed they sit lower, and
      their volume drops to match.
    * Band-limiting to Paula's ceiling costs the broadband effects more than the
      narrow ones, and the loudest effect in this set is broadband — so every
      other volume rises slightly relative to it.  That is the correct balance
      post-band-limiting, not an error.
    """
    loudest = max((s.level for s in samples), default=0.0)
    if loudest <= 0.0:
        return
    for s in samples:
        s.volume = max(1, min(64, int(round(64.0 * s.level / loudest))))


def write_8bit(samples, out_dir: str, *, clock=PAL_CLOCK) -> list[str]:
    """Write .raw files plus a manifest carrying what raw files cannot."""
    import yaml

    os.makedirs(out_dir, exist_ok=True)
    written = []
    entries = {}
    for s in samples:
        path = os.path.join(out_dir, s.name + '.raw')
        with open(path, 'wb') as f:
            f.write(s.data)
        written.append(path)
        entries[s.name] = {
            'file': s.name + '.raw',
            'rate': s.rate,
            'period': s.period,
            'note': s.note,
            'volume': s.volume,
            'finetune': s.finetune,
            'length': len(s.data),
            'repeat_offset': s.repeat_offset,
            'repeat_length': s.repeat_length,
            'channels': ' '.join(t for t, _ in s.channels),
        }

    manifest = {
        'format': 'signed 8-bit mono PCM, headerless',
        'clock': clock,
        'note': ("Trigger each sample at its listed 'note' to hear it at the pitch it "
                 "was rendered at (finetune 0). 'volume' restores the composed "
                 "relative loudness after per-sample peak normalisation. "
                 "'repeat_offset'/'repeat_length' point at a trailing silent word so "
                 "a non-looping sample does not buzz after it finishes."),
        'samples': entries,
    }
    manifest_path = os.path.join(out_dir, 'manifest.yaml')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(manifest, f, sort_keys=False, default_flow_style=False)
    written.append(manifest_path)
    return written
