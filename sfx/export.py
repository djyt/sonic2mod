"""SFX renders as the files sonic2wav writes, as sfx.yaml `output:` asks.

    SfxRender  (stereo float, the chip's rate)
      -> _Shaped    channels folded, DC removed, rate picked, resampled      (each effect)
      -> level      one gain for the set, or each at full scale               (every effect's peak)
      -> SfxExport  quantised to 8 / 16 bits, Paula's silent word appended    (each effect)
      -> write      .wav / .raw files; raw's manifest.yaml holds what a raw file cannot
"""

from __future__ import annotations

import math
import os
import sys
from array import array
from dataclasses import dataclass

from core.audio import dc_block, gain_to_db, resample, to_int8

from .amiga import choose_rate, nearest_candidate, pad_for_paula
from .batch import SfxRender
from .config import FORMAT_RAW, LEVEL_GLOBAL, RATE_AUTO, RATE_NATIVE, OutputSettings
from .wav import write_wav

# A sample's full scale by bit depth: the gain that puts a peak of 1.0 at the rail
_FULL_SCALE = {8: 127.0, 16: 32767.0}
_INT16_MIN, _INT16_MAX = -32768, 32767

# The DC blocker's corner: a one-pole high-pass, not a mean subtraction, since many SFX open with a
# rest a mean would push off zero
_DC_BLOCK_HZ = 30.0

# Paula's (and a MOD's) loudest volume
VOLUME_MAX = 64

MANIFEST = "manifest.yaml"


@dataclass
class _Shaped:
    """One effect before its level: channels at the output rate, still float."""
    render: SfxRender
    channels: list[list[float]]
    rate: int
    period: int | None              # paula: the ProTracker period playing `rate`
    note: str | None                # paula: the note at that period, finetune 0
    peak: float


@dataclass(frozen=True)
class SfxExport:
    """One effect as written."""
    render: SfxRender
    pcm: array                      # signed, interleaved: 'b' 8-bit, 'h' 16-bit
    width: int                      # channels: 1 mono, 2 stereo
    rate: int
    period: int | None
    note: str | None
    volume: int                     # restores the composed balance (normalise), else VOLUME_MAX
    peak_dbfs: float                # the written peak against full scale
    repeat: tuple[int, int] | None  # paula: the silent word's (offset, length) in bytes

    @property
    def name(self) -> str:
        return self.render.name

    @property
    def size(self) -> int:
        return len(self.pcm) * self.pcm.itemsize

    @property
    def seconds(self) -> float:
        return len(self.pcm) / self.width / self.rate if self.rate else 0.0


def export_all(renders: list[SfxRender], out: OutputSettings, *, taps: int, clock: int) -> list[SfxExport]:
    """Every render as `out` writes it.  The level needs the whole set: `global` keeps the composed
    balance with one gain, `normalise` puts each effect at full scale and its volume restores it."""
    shaped = [_shape(r, out, taps, clock) for r in renders]
    loudest = max((s.peak for s in shaped), default=0.0)
    return [_quantise(s, out, loudest) for s in shaped]


def _shape(render: SfxRender, out: OutputSettings, taps: int, clock: int) -> _Shaped:
    """Fold, DC-block, then one resampling stage straight from the chip rate (through 44.1 kHz
    first would add a second stage's error for nothing)."""
    channels = [render.left, render.right] if out.stereo else \
        [[(lv + rv) * 0.5 for lv, rv in zip(render.left, render.right, strict=True)]]
    if out.dc_block:
        channels = [dc_block(c, render.rate, _DC_BLOCK_HZ) for c in channels]

    rate, period, note = _pick_rate(channels[0], render.rate, out, clock)
    if rate != render.rate:
        channels = [resample(c, render.rate, rate, taps=taps) for c in channels]

    peak = max((abs(v) for c in channels for v in c), default=0.0)
    return _Shaped(render, channels, rate, period, note, peak)


def _pick_rate(samples: list[float], rate: int, out: OutputSettings,
               clock: int) -> tuple[int, int | None, str | None]:
    """(rate, period, note): Paula's rates sit on ProTracker's period grid, so a sample plays at
    true pitch with finetune 0; `auto` is the lowest one holding the effect's energy."""
    if out.rate == RATE_NATIVE:
        return rate, None, None
    if not out.paula:
        return int(out.rate), None, None

    if out.rate == RATE_AUTO:
        grid_rate, period, note = choose_rate(samples, rate, max_rate=out.max_rate, clock=clock)
    else:
        grid_rate, period, note = nearest_candidate(int(out.rate), clock)
    return round(grid_rate), period, note


def _quantise(s: _Shaped, out: OutputSettings, loudest: float) -> SfxExport:
    # global: the loudest effect at peak_dbfs, the rest below it as composed; played at full volume.
    # normalise: each at the rails, its volume the composed level (Paula scales after its DAC)
    full = _FULL_SCALE[out.bits]
    if out.level == LEVEL_GLOBAL:
        gain = full * 10.0 ** (out.peak_dbfs / 20.0) / loudest if loudest > 0.0 else 1.0
        volume = VOLUME_MAX
    else:
        gain = full / s.peak if s.peak > 0.0 else 1.0
        volume = max(1, min(VOLUME_MAX, round(VOLUME_MAX * s.peak / loudest))) if loudest > 0.0 else 1

    pcm = _to_pcm8(s.channels, gain, out.dither) if out.bits == 8 else _to_pcm16(s.channels, gain)

    # Paula loops the repeat region once a sample ends: a trailing silent word stops a one-shot buzzing
    repeat = None
    if out.paula:
        data, offset, length = pad_for_paula(pcm.tobytes())
        pcm, repeat = array('b', data), (offset, length)

    written = s.peak * gain
    peak_dbfs = gain_to_db(min(written, full) / full) if written > 0.0 else -math.inf
    return SfxExport(s.render, pcm, len(s.channels), s.rate, s.period, s.note, volume, peak_dbfs, repeat)


def _to_pcm8(channels: list[list[float]], gain: float, dither: str) -> array:
    """Signed 8-bit, each channel dithered as sfx.yaml `dither:` says, interleaved."""
    quantised = [to_int8(c, gain, dither) for c in channels]
    return array('b', bytes(b for frame in zip(*quantised, strict=True) for b in frame))


def _to_pcm16(channels: list[list[float]], gain: float) -> array:
    """Signed 16-bit, rounded (16 bits needs no dither), interleaved."""
    out = array('h', bytes(2 * len(channels) * len(channels[0])))
    width = len(channels)
    for c, samples in enumerate(channels):
        for i, v in enumerate(samples):
            q = round(v * gain)
            out[i * width + c] = _INT16_MAX if q > _INT16_MAX else (_INT16_MIN if q < _INT16_MIN else q)
    return out


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def write_exports(exports: list[SfxExport], out: OutputSettings, directory: str, *, clock: int) -> list[str]:
    """Each effect's file in `directory`; raw files get a manifest.yaml beside them."""
    os.makedirs(directory, exist_ok=True)
    written = []
    for e in exports:
        path = os.path.join(directory, f"{e.name}.{out.format}")
        if out.format == FORMAT_RAW:
            _write_raw(path, e.pcm)
        else:
            write_wav(path, e.pcm, e.width, e.rate)
        written.append(path)

    if out.format == FORMAT_RAW:
        written.append(_write_manifest(exports, out, directory, clock))
    return written


def _write_raw(path: str, pcm: array) -> None:
    """Headerless; 16-bit big-endian, the 68000's order."""
    if pcm.itemsize > 1 and sys.byteorder == "little":
        pcm = array(pcm.typecode, pcm)
        pcm.byteswap()
    with open(path, 'wb') as f:
        f.write(pcm.tobytes())


def _write_manifest(exports: list[SfxExport], out: OutputSettings, directory: str, clock: int) -> str:
    import yaml

    manifest = {
        'format': _format_line(out),
        'clock': clock,
        'note': _manifest_note(out),
        'samples': {e.name: _manifest_entry(e, out) for e in exports},
    }
    path = os.path.join(directory, MANIFEST)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(manifest, f, sort_keys=False, default_flow_style=False)
    return path


def _format_line(out: OutputSettings) -> str:
    """'signed 8-bit mono PCM, headerless'"""
    order = ", big-endian" if out.bits > 8 else ""
    layout = "stereo (interleaved L R)" if out.stereo else "mono"
    return f"signed {out.bits}-bit {layout} PCM, headerless{order}"


def _manifest_note(out: OutputSettings) -> str:
    """What the manifest's fields mean for this output."""
    pitch = ("Trigger each sample at its listed 'note' to hear it at the pitch it "
             "was rendered at (finetune 0). ")
    volume = ("'volume' restores the composed relative loudness after per-sample "
              "peak normalisation. " if out.level != LEVEL_GLOBAL else
              "The composed relative loudness is baked in: play each at 'volume' 64. ")
    repeat = ("'repeat_offset'/'repeat_length' point at a trailing silent word so "
              "a non-looping sample does not buzz after it finishes.")
    return ((pitch if out.paula else "") + volume + (repeat if out.paula else "")).strip()


def _manifest_entry(e: SfxExport, out: OutputSettings) -> dict:
    entry: dict = {'file': f"{e.name}.{out.format}", 'rate': e.rate}
    if out.paula:
        entry |= {'period': e.period, 'note': e.note}
    entry['volume'] = e.volume
    if out.paula:
        entry['finetune'] = 0
    entry['length'] = e.size
    if e.repeat:
        entry |= {'repeat_offset': e.repeat[0], 'repeat_length': e.repeat[1]}
    entry['channels'] = ' '.join(t for t, _ in e.render.channels)
    return entry
