"""configs/sfx.yaml: what sonic2wav renders and how it writes it.

    input:   the SndXX .asm folder --all renders
    render:  the driver run (V-int rate, tail, PSG level)
    output:  folder, container, bits, rate, channels, level, DC, dither, Paula

The chip clocks, the Amiga clock and the resampler's taps are the converter's: settings.yaml.
A `variants:` block lays its keys over the ones beside it (sonic2wav.py --variant amiga).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from core.audio import DITHER_OFF
from core.config import apply_variant, dither_mode, mode_word, read_yaml_file

from .amiga import DEFAULT_MAX_RATE
from .render import DEFAULT_MAX_SECS, DEFAULT_TAIL_SECS, PSG_GAIN

# Paula's (and a MOD's) loudest volume
VOLUME_MAX = 64

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "sfx.yaml"
DEFAULT_INPUT = "reference/smps_drivers/sonic_1/sfx"

FORMAT_WAV, FORMAT_RAW = "wav", "raw"
FORMATS = (FORMAT_WAV, FORMAT_RAW)
BITS = (8, 16)
RATE_NATIVE, RATE_AUTO = "native", "auto"
STEREO, MONO = "stereo", "mono"
CHANNEL_MODES = (STEREO, MONO)
FOLD_MEAN, FOLD_POWER, FOLD_LOUDEST = "mean", "power", "loudest"
MONO_FOLDS = (FOLD_MEAN, FOLD_POWER, FOLD_LOUDEST)
LEVEL_GLOBAL, LEVEL_NORMALISE = "global", "normalise"
LEVELS = (LEVEL_GLOBAL, LEVEL_NORMALISE)
PSG_EXTEND, PSG_CLAMP = "extend", "clamp"
PSG_PAST_TABLE = (PSG_EXTEND, PSG_CLAMP)


@dataclass(frozen=True)
class RenderSettings:
    """sfx.yaml `render:` - the driver run."""
    fps: float = 60.0                       # V-int rate: one SFX tick a frame
    tail_seconds: float = DEFAULT_TAIL_SECS  # rendered after the last track stops
    max_seconds: float = DEFAULT_MAX_SECS   # a safety cap
    psg_gain: float = PSG_GAIN              # PSG against FM, each at its own full scale
    psg_past_table: str = PSG_EXTEND        # a PSG note past the driver's table


@dataclass(frozen=True)
class OutputSettings:
    """sfx.yaml `output:` - what is written."""
    directory: str = "output/sonic_1/sfx"
    format: str = FORMAT_WAV                # wav, or raw: headerless + manifest.yaml
    bits: int = 16
    rate: int | str = 44100                 # Hz, RATE_NATIVE or RATE_AUTO (paula only)
    channels: str = STEREO
    mono_fold: str = FOLD_MEAN              # mono: a panned effect's level against a centred one
    level: str = LEVEL_GLOBAL               # one gain for the set, or each at full scale
    peak_dbfs: float = -0.3                 # global: the loudest effect's peak
    boost: float = 1.0                      # global: the bake asked for (see volume)
    dc_block: bool = False
    dither: str = DITHER_OFF                # 8-bit rounding: core.audio.DITHER_MODES
    paula: bool = False                     # ProTracker period grid, silent repeat word
    max_rate: float = DEFAULT_MAX_RATE      # auto's ceiling

    @property
    def stereo(self) -> bool:
        return self.channels == STEREO

    @property
    def volume(self) -> int:
        """global: the Paula volume the samples play at, 64 / boost to the nearest whole one
        (boost 1.45 -> 44)."""
        return round(VOLUME_MAX / self.boost)

    @property
    def baked(self) -> float:
        """global: the gain baked in, exactly what `volume` undoes (volume 44 -> 64 / 44 = 1.4545)."""
        return VOLUME_MAX / self.volume


@dataclass(frozen=True)
class SfxConfig:
    input: str = DEFAULT_INPUT
    render: RenderSettings = RenderSettings()
    output: OutputSettings = OutputSettings()


def load_sfx_config(path: str | Path = DEFAULT_CONFIG, variant: str | None = None) -> SfxConfig:
    """sfx.yaml as `variant` reads it; every key checked, an unknown one an error."""
    where = str(path)
    data = apply_variant(read_yaml_file(path) or {}, variant, where)
    _check_keys(data, {"input", "render", "output"}, where)

    render = _render(data.get("render") or {}, f"{where}: render")
    output = _output(data.get("output") or {}, f"{where}: output")
    return SfxConfig(input=str(data.get("input", DEFAULT_INPUT)), render=render, output=output)


def _render(data: dict, where: str) -> RenderSettings:
    _check_keys(data, {f.name for f in fields(RenderSettings)}, where)
    d = RenderSettings()
    return RenderSettings(
        fps=_positive(data, "fps", d.fps, where),
        tail_seconds=_positive(data, "tail_seconds", d.tail_seconds, where),
        max_seconds=_positive(data, "max_seconds", d.max_seconds, where),
        psg_gain=_positive(data, "psg_gain", d.psg_gain, where),
        psg_past_table=_word(data, "psg_past_table", d.psg_past_table, PSG_PAST_TABLE, where),
    )


def _output(data: dict, where: str) -> OutputSettings:
    _check_keys(data, {f.name for f in fields(OutputSettings)}, where)
    d = OutputSettings()
    out = OutputSettings(
        directory=str(data.get("directory", d.directory)),
        format=_word(data, "format", d.format, FORMATS, where),
        bits=_choice(data, "bits", d.bits, BITS, where),
        rate=_rate(data.get("rate", d.rate), where),
        channels=_word(data, "channels", d.channels, CHANNEL_MODES, where),
        mono_fold=_word(data, "mono_fold", d.mono_fold, MONO_FOLDS, where),
        level=_word(data, "level", d.level, LEVELS, where),
        peak_dbfs=float(data.get("peak_dbfs", d.peak_dbfs)),
        boost=_positive(data, "boost", d.boost, where),
        dc_block=_flag(data, "dc_block", d.dc_block, where),
        dither=dither_mode(data.get("dither", d.dither), where),
        paula=_flag(data, "paula", d.paula, where),
        max_rate=_positive(data, "max_rate", d.max_rate, where),
    )
    _check_paula(out, where)
    _check_boost(out, where)
    return out


def _check_paula(out: OutputSettings, where: str) -> None:
    """Paula plays 8-bit mono at a period's rate; `auto` picks among those periods only."""
    if out.paula and (out.bits != 8 or out.stereo):
        raise ValueError(f"{where}: paula needs bits: 8 and channels: mono")
    if out.paula and out.rate == RATE_NATIVE:
        raise ValueError(f"{where}: paula needs rate in Hz or auto (native is past Paula's top period)")
    if not out.paula and out.rate == RATE_AUTO:
        raise ValueError(f"{where}: rate: auto picks ProTracker periods: it needs paula: true")


def _check_boost(out: OutputSettings, where: str) -> None:
    """A boost trades headroom for bits; the volume that undoes it is 1 to 64."""
    if out.boost == 1.0:
        return
    if out.level != LEVEL_GLOBAL:
        raise ValueError(f"{where}: boost needs level: global (normalise is at full scale already)")
    if not 1 <= out.volume <= VOLUME_MAX or out.boost < 1.0:
        raise ValueError(f"{where}: boost must be 1 to {VOLUME_MAX} (got {out.boost:g})")


def _check_keys(data: dict, known: set[str], where: str) -> None:
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)} (known: {', '.join(sorted(known))})")


def _positive(data: dict, key: str, default: float, where: str) -> float:
    value = float(data.get(key, default))
    if value <= 0:
        raise ValueError(f"{where}: {key} must be above 0 (got {value:g})")
    return value


def _flag(data: dict, key: str, default: bool, where: str) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key} must be true or false (got {value!r})")
    return value


def _word(data: dict, key: str, default: str, allowed: tuple[str, ...], where: str) -> str:
    return _choice(data, key, default, allowed, where, mode_word)


def _choice(data: dict, key: str, default, allowed: tuple, where: str, read=lambda v: v):
    value = read(data.get(key, default))
    if value not in allowed:
        raise ValueError(f"{where}: {key} must be one of {', '.join(map(str, allowed))} (got {value!r})")
    return value


def _rate(value, where: str) -> int | str:
    """A rate in Hz, or native / auto."""
    if str(value).lower() in (RATE_NATIVE, RATE_AUTO):
        return str(value).lower()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{where}: rate must be Hz, {RATE_NATIVE} or {RATE_AUTO} (got {value!r})")
    return int(value)
