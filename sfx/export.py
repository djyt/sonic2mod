"""SFX renders as the files sonic2wav writes, as sfx.yaml `output:` asks.

    SfxRender  (stereo float, the chip's rate)
      -> _Shaped    channels folded, DC removed, rate picked, resampled      (each effect)
      -> level      one gain for the set (its reference's loudest), or each   (every effect's peak)
                    at full scale
      -> SfxExport  quantised to 8 / 16 bits, Paula's silent word appended    (each effect)
      -> write      .wav / .raw files; raw's manifest.yaml holds what a raw file cannot
"""

from __future__ import annotations

import math
import os
import sys
from array import array
from dataclasses import dataclass

from core.audio import dc_block, fade_end, gain_to_db, resample, sounding_end, sounding_start, to_int8

from .amiga import choose_rate, nearest_candidate, pad_for_paula
from .batch import SfxRender, sound_id
from .config import (
    FOLD_LOUDEST,
    FOLD_MEAN,
    FORMAT_RAW,
    LEVEL_GLOBAL,
    OVER_LIMIT,
    RATE_AUTO,
    RATE_NATIVE,
    VOLUME_MAX,
    OutputSettings,
)
from .slots import SLOTS, slot_mask, slot_names
from .wav import write_wav

# A sample's full scale by bit depth: the gain that puts a peak of 1.0 at the rail
_FULL_SCALE = {8: 127.0, 16: 32767.0}
_INT16_MIN, _INT16_MAX = -32768, 32767

# A mono fold's gain at most: an effect wholly on one side, raised to a centred one's level.  More
# would be out-of-phase content cancelling in the fold
_FOLD_GAIN_MAX = 2.0

# A tail cut fades over a frame: at -30 dB the cut is still a step of a few 8-bit levels
_TAIL_FADE_SECS = 1 / 60

# Half a step rounds to silence: the lead trim keeps from the first sample past it
_HALF_STEP = 0.5

# The DC blocker's corner: a one-pole high-pass, not a mean subtraction, since many SFX open with a
# rest a mean would push off zero
_DC_BLOCK_HZ = 30.0

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
    volume: int                     # normalise: restores the composed balance; global: 64
    peak_dbfs: float                # the written peak against full scale
    clipped: int                    # samples the gain pushed past the rails
    repeat: tuple[int, int] | None  # paula: the silent word's (offset, length) in bytes
    limited_db: float = 0.0         # over: limit: how far the effect was turned down to fit (<= 0)

    @property
    def name(self) -> str:
        return self.render.name

    @property
    def peak(self) -> int:
        """The written peak in steps (127 or 32767 full scale)."""
        return max((abs(v) for v in self.pcm), default=0)

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
    return [_quantise(s, out.for_effect(sound_id(s.render.name)), reference_peak(shaped, out))
            for s in shaped]


def reference_peak(shaped: list[_Shaped], out: OutputSettings) -> float:
    """The peak `global` puts at peak_dbfs: the loudest of `reference`'s effects (the ones a game
    plays most, so a rare loud one does not turn them down), or of the whole set when none of them
    is rendered (one file asked for) or no reference is given."""
    chosen = [s.peak for s in shaped if sound_id(s.render.name) in out.reference]
    return max(chosen or [s.peak for s in shaped], default=0.0)


def _shape(render: SfxRender, out: OutputSettings, taps: int, clock: int) -> _Shaped:
    """Fold, DC-block, then one resampling stage straight from the chip rate (through 44.1 kHz
    first would add a second stage's error for nothing)."""
    channels = [render.left, render.right] if out.stereo else [_fold(render, out.mono_fold)]
    if out.dc_block:
        channels = [dc_block(c, render.rate, _DC_BLOCK_HZ) for c in channels]

    rate, period, note = _pick_rate(channels[0], render.rate, out, clock)
    if rate != render.rate:
        channels = [resample(c, render.rate, rate, taps=taps) for c in channels]

    peak = max((abs(v) for c in channels for v in c), default=0.0)
    return _Shaped(render, channels, rate, period, note, peak)


def _fold(render: SfxRender, fold: str) -> list[float]:
    """Mono, (L + R) / 2, raised by `fold` for a panned effect (Sonic 1's: the rings, B5 right, CE left):

        mean     no gain: one side's sound 6 dB under a centred one (the Mega Drive's mono A/V out)
        power    the stereo power kept: 3 dB under (what a stereo listener hears)
        loudest  the louder side's power: as loud as a centred one

    A centred effect is the same in all three."""
    mono = [(lv + rv) * 0.5 for lv, rv in zip(render.left, render.right, strict=True)]
    if fold == FOLD_MEAN:
        return mono

    power_left = math.fsum(v * v for v in render.left)
    power_right = math.fsum(v * v for v in render.right)
    power_mono = math.fsum(v * v for v in mono)
    if power_mono <= 0.0:
        return mono
    target = max(power_left, power_right) if fold == FOLD_LOUDEST else (power_left + power_right) / 2
    gain = min(math.sqrt(target / power_mono), _FOLD_GAIN_MAX)
    return [v * gain for v in mono]


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
    # global: the reference's loudest at peak_dbfs, the rest as composed, played at volume 64 (a bake
    # for a lower volume gains nothing: Paula scales the grain with the signal).  normalise: each at
    # the rails, its volume the composed level (Paula scales after its DAC)
    full = _FULL_SCALE[out.bits]
    limited_db = 0.0
    if out.level == LEVEL_GLOBAL:
        gain = full * 10.0 ** (out.peak_dbfs / 20.0) / loudest if loudest > 0.0 else 1.0
        volume = VOLUME_MAX
        if out.over == OVER_LIMIT and s.peak * gain > full:
            # turned down to just fit: quieter than composed, never clipped (the tail cut is the
            # effect's own peak's, so it falls where it did; the lead trim's step moves with the gain)
            limited_db = gain_to_db(full / (s.peak * gain))
            gain = full / s.peak
    else:
        gain = full / s.peak if s.peak > 0.0 else 1.0
        volume = max(1, min(VOLUME_MAX, round(VOLUME_MAX * s.peak / loudest))) if loudest > 0.0 else 1

    channels = _trim(s.channels, gain, s.peak, out, s.rate)
    pcm = _to_pcm8(channels, gain, out.dither) if out.bits == 8 else _to_pcm16(channels, gain)
    clipped = sum(1 for c in channels for v in c if abs(v * gain) >= full + 0.5)

    # Paula loops the repeat region once a sample ends: a trailing silent word stops a one-shot buzzing
    repeat = None
    if out.paula:
        data, offset, length = pad_for_paula(pcm.tobytes())
        pcm, repeat = array('b', data), (offset, length)

    written = s.peak * gain
    peak_dbfs = gain_to_db(min(written, full) / full) if written > 0.0 else -math.inf
    return SfxExport(s.render, pcm, len(channels), s.rate, s.period, s.note, volume, peak_dbfs, clipped,
                     repeat, limited_db)


def _trim(channels: list[list[float]], gain: float, peak: float, out: OutputSettings,
          rate: int) -> list[list[float]]:
    """What of an effect is kept:

        trim_lead  from the first sample that rounds to a step: 19 of Sonic 1's SFX open on a rest
                   (nRst, $01: the voice keyed off a frame before its note), silence in a sample
        tail_db    to the last sample within tail_db of the effect's own peak, its last frame faded
    """
    n = len(channels[0])
    start = min(sounding_start(c, _HALF_STEP / gain) for c in channels) if out.trim_lead else 0
    end = max(sounding_end(c, peak * 10.0 ** (out.tail_db / 20.0)) for c in channels) \
        if out.tail_db is not None else n
    if start >= end or (start, end) == (0, n):
        return channels

    kept = [c[start:end] for c in channels]
    if end == n:
        return kept
    return [fade_end(c, len(c), rate, _TAIL_FADE_SECS) for c in kept]


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
        'mask_bits': ' '.join(SLOTS) + ' (bit 0 first)',
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
              f"The composed relative loudness is baked in: play each at 'volume' {VOLUME_MAX}. ")
    slots = ("'slots'/'mask': the SFX track slots the effect takes (PSG3 tone and noise one). "
             "'peak': the written peak in steps; 'clipped': samples past the rails. ")
    if out.over == OVER_LIMIT:
        slots += "'limited_db': how far an effect past the rails was turned down to fit. "
    repeat = ("'repeat_offset'/'repeat_length' point at a trailing silent word so "
              "a non-looping sample does not buzz after it finishes.")
    return ((pitch if out.paula else "") + volume + slots + (repeat if out.paula else "")).strip()


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
    hws = [hw for _, hw in e.render.channels]
    entry |= {
        'channels': ' '.join(t for t, _ in e.render.channels),
        'slots': ' '.join(slot_names(hws)),
        'mask': slot_mask(hws),
        'peak': e.peak,
        'clipped': e.clipped,
    }
    if out.over == OVER_LIMIT:
        entry['limited_db'] = round(e.limited_db, 1)
    return entry
