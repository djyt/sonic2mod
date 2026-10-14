"""Discovery and batch rendering, at the chip's rate (sfx/export.py writes them)."""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from core.audio import DEFAULT_TAPS
from core.chips import MD_FM_CLOCK, MD_PSG_CLOCK
from core.chips.sn76489 import SN76489
from core.chips.ym2612 import OPN2, output_rate
from core.drivers import locate_sounds, read_rom_song
from core.drivers.reference import SONIC1_RULES
from core.rom import RomImage
from core.smps import SmpsParser, SmpsSong

from .render import PSG_GAIN, render_sfx

_NAME_RE = re.compile(r'^Snd([0-9A-Fa-f]{2})\s*(?:-\s*(.*))?$')


class SfxRender:
    """One rendered SFX at the chip's rate, held in memory until the set's peak is known."""

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
    return [SfxSource(output_name(p), p, lambda p=p: SmpsParser(SONIC1_RULES).parse_file(p)) for p in paths]


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
               psg_gain=PSG_GAIN, psg_oob="extend", native_rate: int = OPN2.NATIVE_RATE,
               psg_oversample: int = 1, taps: int = DEFAULT_TAPS) -> SfxRender:
    """Read and render a single SFX."""
    song = source.read()
    warnings: list[str] = []

    if not song.header.is_sfx:
        warnings.append("no SFX header found — parsed as music, output may be wrong")
    if not song.channels:
        raise ValueError(f"{os.path.basename(source.label)}: no channels found")

    result = render_sfx(song, opn2, sn, fps=fps, tail_secs=tail_secs,
                        max_secs=max_secs, psg_gain=psg_gain, psg_oob=psg_oob, native_rate=native_rate,
                        psg_oversample=psg_oversample, taps=taps)
    warnings.extend(result.warnings)
    if result.truncated:
        warnings.append(f"hit the {max_secs:g}s cap — output truncated")

    return SfxRender(
        path=source.label,
        name=source.name,
        left=result.left,
        right=result.right,
        rate=result.rate,
        ticks=result.ticks,
        channels=[(c.header.channel_type, c.header.hw_channel) for c in song.channels],
        warnings=warnings,
        truncated=result.truncated,
    )


def render_all(sources, *, fps=60.0, tail_secs=1.0, max_secs=10.0, psg_gain=PSG_GAIN,
               psg_oob="extend", fm_clock=MD_FM_CLOCK, psg_clock=MD_PSG_CLOCK,
               psg_oversample: int = 1, taps: int = DEFAULT_TAPS, progress=None) -> list[SfxRender]:
    """Render every source with a single shared pair of chip instances, clocked at `fm_clock` /
    `psg_clock` (settings.yaml fm_synthesis / psg_synthesis clock_rate), the PSG at
    `psg_oversample` x the FM's rate (psg_synthesis.oversample)."""
    native_rate = output_rate(fm_clock)
    opn2 = OPN2(mode="ym2612")
    sn = SN76489(clock_rate=psg_clock, sample_rate=native_rate * psg_oversample)
    try:
        renders = []
        for source in sources:
            if progress is not None:
                progress(source)
            renders.append(render_one(
                source, opn2, sn, fps=fps, tail_secs=tail_secs, max_secs=max_secs,
                psg_gain=psg_gain, psg_oob=psg_oob, native_rate=native_rate,
                psg_oversample=psg_oversample, taps=taps,
            ))
        return renders
    finally:
        sn.shutdown()
