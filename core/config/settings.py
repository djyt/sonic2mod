"""settings.yaml: how the samples are synthesised (SynthesisSettings for the YM2612,
PsgSynthesisSettings for the SN76489) and the MOD-wide choices (legato, player)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..audio import DEFAULT_DITHER, DEFAULT_TAPS
from ..mod import PAL_AMIGA_CLOCK, sample_limit_bytes
from ..smps import DEFAULT_FM_PAN_LAW_DB, MD_FM_CLOCK, MD_PSG_CLOCK
from .loader import dither_mode, mode_word, read_yaml_file

if TYPE_CHECKING:
    from .song import ConversionConfig


def _psg_volume_mode(value) -> str:
    v = str(value).strip().lower()
    if v not in ("baked", "absolute"):
        raise ValueError(f"psg_volume_scaling must be baked or absolute (got '{value}')")
    return v


# settings.yaml `samples:` keys; each was top level before it
SAMPLE_KEYS = ("max_sample_kb", "pt_zero_bytes", "dither", "dc_block", "sustain_loops", "loop_drift_db",
               "treble_shelf_db", "treble_shelf_hz", "resample_taps", "render_cache")


# settings.yaml's keys, by section (None: the top level)
_KEYS = {
    None: {"fm_synthesis", "psg_synthesis", "samples", "amiga_clock", "fm_volume_scaling", "fm_pan_law_db",
           "psg_volume_scaling", "legato", "player"},
    "fm_synthesis": {"enabled", "mode", "clock_rate", "sustain_duration", "release_padding", "detune_variants",
                     "threads"},
    "psg_synthesis": {"enabled", "clock_rate", "sustain_duration", "release_padding", "oversample"},
    "samples": set(SAMPLE_KEYS),
}


def _check_keys(data: dict, filepath: str) -> None:
    """A key settings.yaml does not know is an error: a typo, or a retired key, would be ignored."""
    for section, known in _KEYS.items():
        keys = data if section is None else (data.get(section) or {})
        unknown = sorted(set(keys) - known)
        if unknown:
            where = "" if section is None else f"{section}."
            raise ValueError(f"{filepath}: unknown key(s): {', '.join(where + k for k in unknown)}")


def _samples_section(data: dict, filepath: str) -> dict:
    """settings.yaml `samples:`, its keys checked."""
    _check_keys(data, filepath)
    return dict(data.get("samples") or {})


def _max_sample_kb(data: dict, default: int, filepath: str) -> int:
    """`samples.max_sample_kb` of settings.yaml, validated."""
    kb = data.get("max_sample_kb", default)
    try:
        sample_limit_bytes(kb)
    except ValueError as e:
        raise ValueError(f"{filepath}: {e}") from e
    return kb


def _sample_flag(data: dict, key: str, default: bool, filepath: str) -> bool:
    """A true / false key of settings.yaml `samples:`."""
    v = data.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"{filepath}: {key} must be true or false (got {v!r})")
    return v


SUSTAIN_LOOP_MODES = ("off", "merged", "all")


def _sustain_loops(data: dict, default: str, filepath: str) -> str:
    """`samples.sustain_loops` of settings.yaml: off | merged | all."""
    v = mode_word(data.get("sustain_loops", default))
    if v not in SUSTAIN_LOOP_MODES:
        raise ValueError(f"{filepath}: sustain_loops must be one of {', '.join(SUSTAIN_LOOP_MODES)} (got '{v}')")
    return v


LEGATO_MODES = ("strict", "loose", "retrigger")


def _legato(data: dict, default: str, filepath: str) -> str:
    """Top-level `legato` of settings.yaml: retrigger | strict | loose."""
    v = str(data.get("legato", default)).lower()
    if v not in LEGATO_MODES:
        raise ValueError(f"{filepath}: legato must be one of {', '.join(LEGATO_MODES)} (got '{v}')")
    return v


# The tracker a build is made for (settings.yaml `player`): FT2 clone and ProTracker 2 scale a 4xy
# depth differently, see vibrato_depth
PLAYERS = ("ft2", "pt2")


def _player(data: dict, default: str, filepath: str) -> str:
    """Top-level `player` of settings.yaml: ft2 | pt2."""
    v = str(data.get("player", default)).lower()
    if v not in PLAYERS:
        raise ValueError(f"{filepath}: player must be one of {', '.join(PLAYERS)} (got '{v}')")
    return v


# The treble shelf's corner (settings.yaml treble_shelf_hz), at the sample's own pitch
DEFAULT_SHELF_HZ = 2500.0


# PSG tones render at this multiple of the sample's rate (settings.yaml psg_synthesis.oversample)
DEFAULT_PSG_OVERSAMPLE = 8


# PAL Amiga Paula clock (settings.yaml amiga_clock): a MOD note's rate is this / its period
DEFAULT_AMIGA_CLOCK = PAL_AMIGA_CLOCK

# The chip clocks (settings.yaml fm_synthesis / psg_synthesis clock_rate): the NTSC Mega Drive's
DEFAULT_FM_CLOCK = MD_FM_CLOCK
DEFAULT_PSG_CLOCK = MD_PSG_CLOCK


def _positive_int(data: dict, key: str, default: int, filepath: str, even: bool = False) -> int:
    """A settings.yaml count in `data`: an integer >= 1 (even when `even`)."""
    try:
        v = int(data.get(key, default))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{filepath}: {key} must be an integer") from e
    if v < 1 or (even and v % 2):
        raise ValueError(f"{filepath}: {key} must be {'an even' if even else 'a'} whole number >= 1 (got {v})")
    return v


def _treble_shelf(data: dict, defaults: tuple[float, float], filepath: str) -> tuple[float, float]:
    """`samples.treble_shelf_db` / `treble_shelf_hz` of settings.yaml: (gain, corner) of the
    optional high shelf on every synthesised render (core.audio.pcm.high_shelf); 0 dB = off."""
    try:
        return float(data.get("treble_shelf_db", defaults[0])), float(data.get("treble_shelf_hz", defaults[1]))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{filepath}: treble_shelf_db / treble_shelf_hz must be numbers") from e


def _loop_drift_db(data: dict, default: float, filepath: str) -> float:
    """`samples.loop_drift_db` of settings.yaml: how far a looped sample's level may sit above
    where the instrument's longest note would have decayed to."""
    try:
        v = float(data.get("loop_drift_db", default))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{filepath}: loop_drift_db must be a number of dB") from e
    if v < 0:
        raise ValueError(f"{filepath}: loop_drift_db must not be negative (got {v})")
    return v


# The project root: configs/ and a relative samples.render_cache are read from it
_ROOT = Path(__file__).resolve().parents[2]


def _render_cache(data: dict) -> str | None:
    """samples.render_cache: the directory chip renders are kept in (relative to the project), or off."""
    value = data.get("render_cache")
    if value is None or mode_word(value) == "off":
        return None
    path = Path(str(value))
    return str(path if path.is_absolute() else _ROOT / path)


def _sustain_duration(section: dict, default: float | str) -> float | str:
    """A synthesis section's sustain_duration: seconds, or "auto" (each instrument its longest ring)."""
    sd = section.get("sustain_duration", default)
    return sd if sd == "auto" else float(sd)


@dataclass
class SampleSettings:
    """What both chips' samples share: settings.yaml `samples:` and `amiga_clock`, and the sustain
    the converter resolves per instrument (core/convert/sustain_plan.py)."""
    enabled: bool = False
    amiga_clock: int = DEFAULT_AMIGA_CLOCK   # settings.yaml amiga_clock (top level)
    # `auto` resolved: {instrument: seconds}, each instrument's own longest ring (the converter's
    # SustainPlanner.resolve); an instrument absent here gets sustain_duration.  Empty when a number is stated.
    sustain_by_instrument: dict = field(default_factory=dict)
    slide_ends: frozenset = frozenset()      # instruments a note of ends in a release slide (merged
                                             # build): only they are heard past their sustain
    exact_sustain: frozenset = frozenset()   # instruments whose auto sustain holds every note that
                                             # plays them: the sample ends where those notes stop
                                             # being heard (the release padding only a cut-short
                                             # note could reach is left off)
    max_sample_kb: int = 128         # settings.yaml samples.max_sample_kb: 128 = the format's limit, 64 = ProTracker's
    # settings.yaml samples.sustain_loops: which builds cut each settled sample to a loop and
    # end its notes with a release slide (core.audio.loops) - "off", "merged" (--merged only), "all".
    sustain_loops: str = "merged"
    loop_drift_db: float = 1.0       # settings.yaml samples.loop_drift_db: dB a loop may freeze above the
                                     # level the longest note would have decayed to (core.audio.loops)
    treble_shelf_db: float = 0.0     # settings.yaml samples.treble_shelf_db: brightness shelf, 0 = off
    treble_shelf_hz: float = DEFAULT_SHELF_HZ   # settings.yaml samples.treble_shelf_hz: its corner
    resample_taps: int = DEFAULT_TAPS  # settings.yaml samples.resample_taps: filter width, at the lower rate
    dither: str = DEFAULT_DITHER     # settings.yaml samples.dither (core.audio.pcm.DITHER_MODES)
    dc_block: bool = False           # settings.yaml samples.dc_block: each render's DC removed (core.audio.pcm.dc_block)
    render_cache: str | None = None  # settings.yaml samples.render_cache: where chip renders are kept
                                     # (core/render_cache.py); None = off

    @property
    def max_sample_bytes(self) -> int:
        """Bytes one synthesised sample may hold (core.mod.limits.sample_limit_bytes)."""
        return sample_limit_bytes(self.max_sample_kb)

    def loops_for(self, merged: bool) -> bool:
        """True when this build (the merged one or the reference) gets sustain loops."""
        return self.sustain_loops == "all" or (self.sustain_loops == "merged" and merged)

    @classmethod
    def _shared_fields(cls, data: dict, smp: dict, filepath: str) -> dict:
        """The shared fields from settings.yaml: `data` the whole file, `smp` its `samples:`
        (_samples_section).  A key the file leaves out keeps the field's default."""
        shelf_db, shelf_hz = _treble_shelf(smp, (cls.treble_shelf_db, cls.treble_shelf_hz), filepath)
        return dict(
            amiga_clock=int(data.get("amiga_clock", cls.amiga_clock)),
            max_sample_kb=_max_sample_kb(smp, cls.max_sample_kb, filepath),
            sustain_loops=_sustain_loops(smp, cls.sustain_loops, filepath),
            loop_drift_db=_loop_drift_db(smp, cls.loop_drift_db, filepath),
            treble_shelf_db=shelf_db,
            treble_shelf_hz=shelf_hz,
            resample_taps=_positive_int(smp, "resample_taps", cls.resample_taps, filepath, even=True),
            dc_block=_sample_flag(smp, "dc_block", cls.dc_block, filepath),
            dither=dither_mode(smp.get("dither", cls.dither), f"{filepath}: samples"),
            render_cache=_render_cache(smp),
        )


@dataclass
class PsgSynthesisSettings(SampleSettings):
    clock_rate: int = DEFAULT_PSG_CLOCK   # SN76489 clock (Hz)
    sustain_duration: float | str = 1.0
    release_padding: float = 0.2
    # Envelope tables are not a setting: the driver's own live in core.smps.driver_tables.PSG_ENVELOPES_BY_NAME.
    # PSG level model.  "baked": per instrument, the attenuation most of its notes play at needs no
    # command and is what the sample_list volume stands for; other notes get Cxx on the chip's
    # 2 dB/step law (same scheme as SynthesisSettings.fm_volume_mode).  "absolute": legacy —
    # volume = 64 × 10^(−2·att/20) × sample volume / 64, so a Cxx on nearly every PSG note.
    psg_volume_scaling: str = "baked"
    psg_oversample: int = DEFAULT_PSG_OVERSAMPLE   # settings.yaml psg_synthesis.oversample

    @classmethod
    def from_yaml(cls, filepath: str) -> 'PsgSynthesisSettings':
        data = read_yaml_file(filepath)
        s = data.get("psg_synthesis", {})
        smp = _samples_section(data, filepath)
        return cls(
            enabled=s.get("enabled", cls.enabled),
            clock_rate=s.get("clock_rate", cls.clock_rate),
            sustain_duration=_sustain_duration(s, cls.sustain_duration),
            release_padding=s.get("release_padding", cls.release_padding),
            psg_volume_scaling=_psg_volume_mode(data.get("psg_volume_scaling", cls.psg_volume_scaling)),
            psg_oversample=_positive_int(s, "oversample", cls.psg_oversample, filepath),
            **cls._shared_fields(data, smp, filepath),
        )


@dataclass
class SynthesisSettings(SampleSettings):
    mode: str = "ym2612"
    clock_rate: int = DEFAULT_FM_CLOCK    # YM2612 master clock (Hz)
    sustain_duration: float | str = 1.5
    release_padding: float = 0.5
    threads: int | str = "normal"     # Render threads: "normal" (cores − 1), "max" (all cores), or a count
    detune_variants: bool = True      # an smpsAlterNote note plays a sample rendered at its FNUM offset (core.plan.detune)
    # FM level model — see fm_volume_mode.  "baked" | True ("absolute") | False ("off").
    fm_volume_scaling: bool | str = "baked"
    fm_pan_law_db: float = DEFAULT_FM_PAN_LAW_DB   # "baked" mode: a hard-panned note is this many dB below a centred one
    # settings.yaml `legato` (top level): how an smpsNoAttack note is written when its target cannot
    # ride the sounding sample - "strict" (another range: the sounding sample, note moved by the
    # chip-pitch delta; after smpsSetvoice or with nothing sounding: a re-trigger; what FT2 clone and
    # ProTracker need), "loose" (always 3FF on the target's own instrument, as written before) or
    # "retrigger" (every no-attack note a plain note-on, as before 030ca81; the default).
    legato: str = "retrigger"
    # settings.yaml `player` (top level): the tracker the MOD is made for, "ft2" (default) or "pt2"
    player: str = "ft2"
    # settings.yaml samples.pt_zero_bytes: a one-shot sample's first word zeroed, since ProTracker
    # replays it once the sample ends (core.mod.ModFile.zero_idle_words)
    pt_zero_bytes: bool = True

    @property
    def fm_volume_mode(self) -> str:
        """How FM channel levels (smpsHeaderFM volume, smpsAlterVol, smpsPan) reach the MOD.

        "baked"    — per instrument, the level most of its notes play at needs no command (it is
                     what the sample_list volume stands for); any other level gets
                     Cxx = volume × 10^(ΔdB/20), ΔdB from the chip's 0.75 dB/TL step and the pan
                     law.  Cxx only on the minority channel / after smpsAlterVol.  Default.
        "absolute" — (legacy `true`) header TL + log law as an absolute volume: Cxx on every note.
        "off"      — (legacy `false`) header TL ignored, one smpsAlterVol step = one linear MOD
                     volume unit.  Wrong by up to several dB on faded notes; kept for comparison.
        """
        v = self.fm_volume_scaling
        if isinstance(v, str):
            v = v.strip().lower()
            if v in ("baked", "absolute", "off"):
                return v
            raise ValueError(f"fm_volume_scaling must be baked, true or false (got '{self.fm_volume_scaling}')")
        return "absolute" if v else "off"

    def worker_threads(self) -> int:
        """How many FM instruments render at once (see `threads` in settings.yaml).

        "normal" — one thread per CPU core but one, so a conversion leaves a core for
                   whatever else the machine is doing (never below 1).  Default.
        "max"    — one thread per core.
        n        — exactly n threads; 1 renders the instruments one after another.
        The rendered samples do not depend on this: each thread owns its own chip and the
        results are consumed in a fixed order.
        """
        cores = os.cpu_count() or 1
        v = self.threads
        if isinstance(v, str):
            key = v.strip().lower()
            if key == "normal":
                return max(1, cores - 1)
            if key == "max":
                return cores
            if key.isdigit():
                v = int(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(
                f"fm_synthesis.threads must be 'normal', 'max' or a positive integer (got {self.threads!r})")
        return v

    @classmethod
    def from_yaml(cls, filepath: str) -> "SynthesisSettings":
        data = read_yaml_file(filepath)
        s = data.get("fm_synthesis", {})
        smp = _samples_section(data, filepath)
        return cls(
            enabled=s.get("enabled", cls.enabled),
            mode=s.get("mode", cls.mode),
            clock_rate=s.get("clock_rate", cls.clock_rate),
            sustain_duration=_sustain_duration(s, cls.sustain_duration),
            release_padding=s.get("release_padding", cls.release_padding),
            threads=s.get("threads", cls.threads),
            detune_variants=bool(s.get("detune_variants", cls.detune_variants)),
            fm_volume_scaling=data.get("fm_volume_scaling", cls.fm_volume_scaling),
            fm_pan_law_db=float(data.get("fm_pan_law_db", cls.fm_pan_law_db)),
            legato=_legato(data, cls.legato, filepath),
            player=_player(data, cls.player, filepath),
            pt_zero_bytes=_sample_flag(smp, "pt_zero_bytes", cls.pt_zero_bytes, filepath),
            **cls._shared_fields(data, smp, filepath),
        )


def with_song_overrides(settings, config: "ConversionConfig"):
    """`settings` (Synthesis- or PsgSynthesisSettings) with the song's own overrides applied:
    loop_drift_db, treble_shelf_db."""
    import dataclasses
    if config.loop_drift_db is not None:
        settings = dataclasses.replace(settings, loop_drift_db=config.loop_drift_db)
    if config.treble_shelf_db is not None:
        settings = dataclasses.replace(settings, treble_shelf_db=config.treble_shelf_db)
    return settings


# The settings file a tool reads when no song config sits beside one
_REPO_SETTINGS = _ROOT / "configs" / "settings.yaml"


def find_settings(config_path: str | None = None) -> str | None:
    """The global settings file: settings.yaml beside the song config (both live in configs/), else
    configs/settings.yaml; None when neither exists."""
    beside = [Path(config_path).resolve().parent / "settings.yaml"] if config_path else []
    for path in [*beside, _REPO_SETTINGS]:
        if path.exists():
            return str(path)
    return None


def load_settings(path: str | None) -> tuple["SynthesisSettings", "PsgSynthesisSettings"]:
    """Both chips' settings from `path`; the code's defaults when there is no file."""
    if path is None or not os.path.exists(path):
        return SynthesisSettings(), PsgSynthesisSettings()
    return SynthesisSettings.from_yaml(path), PsgSynthesisSettings.from_yaml(path)
