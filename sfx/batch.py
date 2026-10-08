"""Discovery, batch rendering and global normalisation."""

from __future__ import annotations

import glob
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from core.audio import gain_to_db
from core.chips import MD_FM_CLOCK, MD_PSG_CLOCK
from core.drivers import locate_sounds, read_rom_song
from core.mod import PAL_AMIGA_CLOCK
from core.rom import RomImage
from core.smps import SmpsParser, SmpsSong
from sn76489.wrapper import SN76489
from ym2612.wrapper import OPN2, output_rate

from .amiga import (
    DEFAULT_MAX_RATE,
    choose_rate,
    dc_block,
    nearest_candidate,
    normalise,
    pad_for_paula,
    quantise_8bit,
)
from .render import render_sfx
from .resample import DEFAULT_TAPS, resample, resample_stereo
from .wav import write_wav

_NAME_RE = re.compile(r'^Snd([0-9A-Fa-f]{2})\s*(?:-\s*(.*))?$')


class SfxRender:
    """One rendered SFX, held in memory until the global peak is known."""

    __slots__ = (
        "channels",
        "left",
        "name",
        "path",
        "peak",
        "rate",
        "right",
        "ticks",
        "truncated",
        "warnings",
    )

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


@dataclass(frozen=True)
class SfxSource:
    """One SFX to render: an asm file, or a sound in a ROM."""
    name: str                       # the output file's stem: 'B5_Ring', 'B5'
    label: str                      # what the report calls it: its path, or 'ROM $B5'
    read: Callable[[], SmpsSong]


def asm_sources(paths) -> list[SfxSource]:
    return [SfxSource(output_name(p), p, lambda p=p: SmpsParser().parse_file(p)) for p in paths]


def rom_sources(rom_path: str) -> list[SfxSource]:
    """Every SFX the ROM's indexes name ($A0 ..., the special $D0), with the data fixes known for
    that ROM (as the asm's)."""
    rom = RomImage.load(rom_path)
    index = locate_sounds(rom)
    return [SfxSource(f"{sid:02X}", f"ROM ${sid:02X}", lambda sid=sid: read_rom_song(rom, sid, index))
            for sid in sorted(index.sfx)]


def discover(sfx_dir: str) -> list[str]:
    """Return the SFX .asm files in driver order.

    Raises FileNotFoundError with an actionable message when the directory is
    absent — `reference/smps_drivers` is gitignored, so a fresh clone will not have it.
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


def render_one(source: SfxSource, opn2, sn, *, fps=60.0, tail_secs=1.0, max_secs=10.0,
               psg_gain=1.0, psg_oob="extend", target_rate: int | None = 44100,
               taps=DEFAULT_TAPS, native_rate: int = OPN2.NATIVE_RATE) -> SfxRender:
    """Read, render and resample a single SFX."""
    song = source.read()
    warnings: list[str] = []

    if not song.header.is_sfx:
        warnings.append("no SFX header found — parsed as music, output may be wrong")
    if not song.channels:
        raise ValueError(f"{os.path.basename(source.label)}: no channels found")

    result = render_sfx(song, opn2, sn, fps=fps, tail_secs=tail_secs,
                        max_secs=max_secs, psg_gain=psg_gain, psg_oob=psg_oob, native_rate=native_rate)
    warnings.extend(result.warnings)
    if result.truncated:
        warnings.append(f"hit the {max_secs:g}s cap — output truncated")

    left, right = result.left, result.right
    rate = result.rate
    if target_rate and target_rate != rate:
        left, right = resample_stereo(left, right, rate, target_rate, taps=taps)
        rate = target_rate

    return SfxRender(
        path=source.label,
        name=source.name,
        left=left,
        right=right,
        rate=rate,
        ticks=result.ticks,
        channels=[(c.header.channel_type, c.header.hw_channel) for c in song.channels],
        warnings=warnings,
        truncated=result.truncated,
    )


def render_all(sources, *, fps=60.0, tail_secs=1.0, max_secs=10.0, psg_gain=1.0,
               psg_oob="extend", target_rate: int | None = 44100, taps=DEFAULT_TAPS,
               fm_clock=MD_FM_CLOCK, psg_clock=MD_PSG_CLOCK, progress=None) -> list[SfxRender]:
    """Render every source with a single shared pair of chip instances, clocked at `fm_clock` /
    `psg_clock` (settings.yaml fm_synthesis / psg_synthesis clock_rate)."""
    native_rate = output_rate(fm_clock)
    opn2 = OPN2(mode="ym2612")
    sn = SN76489(clock_rate=psg_clock, sample_rate=native_rate)
    try:
        renders = []
        for source in sources:
            if progress is not None:
                progress(source)
            renders.append(render_one(
                source, opn2, sn, fps=fps, tail_secs=tail_secs, max_secs=max_secs,
                psg_gain=psg_gain, psg_oob=psg_oob, target_rate=target_rate, taps=taps,
                native_rate=native_rate,
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
    return gain_to_db(min(value, 32767.0) / 32767.0)


# ---------------------------------------------------------------------------
# 8-bit Amiga export
# ---------------------------------------------------------------------------

class AmigaSample:
    """One effect prepared as an 8-bit Paula sample."""

    __slots__ = (
        "channels",
        "data",
        "finetune",
        "level",
        "name",
        "note",
        "period",
        "rate",
        "repeat_length",
        "repeat_offset",
        "volume",
        "warnings",
    )

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
                 clock=PAL_AMIGA_CLOCK, energy_frac=0.99) -> AmigaSample:
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

    target = round(rate)
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
        s.volume = max(1, min(64, round(64.0 * s.level / loudest)))


def write_8bit(samples, out_dir: str, *, clock=PAL_AMIGA_CLOCK) -> list[str]:
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
