"""A minimal config completed from its song: every section the user did not state is derived
(docs/todo/binary_import.md 2.8).  The result is the YAML a hand-written config would hold, run
through the same parsers, so derived and stated configs are read alike and `convert.py
--show-config` prints exactly what was used.

    stated by the user      name, input_file, rom_song, merge choices, by-ear tweaks, overrides
    derived from the song   range_space: chip, channels, timing, voice_map, psg_voice_map, psg_map,
                            dac_samples, sample_list, samples_dir, output_file

An override replaces the item it names (one voice's entries, one envelope's, one DAC sample,
one sample_list row), never the whole section.

Instruments, in slot order:  DAC samples · FM voices · PSG tones · PSG noise
    FM / PSG tone   one per three-octave window of the chip pitches a voice / envelope plays,
                    its lowest at E1 (the first MOD note above the audit's 5 kHz low-rate line)
                    or as high as a wider window allows; the converter picks the rendering pitch
    DAC             one per sample; a pitched copy plays its sample's slot at the note nearest
                    its rate
    volumes         starting_volume: every synthesised sample is peak-normalised, so its volume
                    carries the level its notes mostly play at (TL offset and pan, or attenuation),
                    until vgm_compare --write-volumes sets it from a rip
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..audio import db_to_gain
from ..chips import DEFAULT_FM_PAN_LAW_DB, fm_level_db, psg_level_db
from ..config import ChannelConfig, ConversionConfig, bpm_rounding_options, find_settings, load_settings
from ..mod import LOW_RATE_HZ, PERIOD_TABLE, ModNote
from ..rom import DacSample
from ..smps import SmpsSong, note_label, source_map, synth_note_name
from ..source import read_dac
from .driver_state import walk_channel

MAX_INSTRUMENTS = 31
_MOD_C1 = 12                 # synth_note_name's semitone for MOD C1
_MOD_SPAN = 35               # C1 ... B3
_NOISE_ROOT = "A3"           # a noise sample rendered at ~28 kHz keeps most of its hiss (Title Screen)
_ROWS_PER_PATTERN = 64
_FIRST_NOTE = 0x81           # the note byte of C0
_FINETUNES = range(-8, 8)    # a MOD sample's finetune
_FINETUNE_STEPS = 96         # finetune steps an octave
_ROW_FINETUNE = 3            # sample_list row: [instrument, file, volume, finetune]

# Starting sample_list volumes at TL offset 0 / attenuation 0 (calibrated on Green Hill Zone)
_FM_SCALE = 76.0
_PSG_SCALE = 16.0
_FULL = 64


def starting_volume(kind: str, level: int = 0, hard_panned: bool = False,
                    pan_law_db: float = DEFAULT_FM_PAN_LAW_DB) -> int:
    """A sample_list volume to start from: a sample is peak-normalised, so the volume carries the
    level its notes mostly play at.  `kind` FM (level: TL offset), tone / noise (attenuation), DAC."""
    if kind == "DAC":
        return _FULL
    if kind == "FM":
        gain = _FM_SCALE * db_to_gain(fm_level_db(level, hard_panned, pan_law_db))
    else:
        gain = _PSG_SCALE * db_to_gain(psg_level_db(level))
    return max(1, min(_FULL, round(gain)))

@dataclass
class Derivation:
    data: dict                                              # the config's YAML, stated and derived
    derived: list[str] = field(default_factory=list)        # the sections the song filled
    files: dict[str, bytes] = field(default_factory=dict)   # samples_dir files to write (DAC samples)


def load_config(path: str | Path, settings_path: str | None = None) -> ConversionConfig:
    """A config file, a minimal one completed from its song (what every tool reads a config with)."""
    config = ConversionConfig.from_yaml(str(path))
    if config.is_minimal:
        clock = load_settings(settings_path or find_settings(str(path)))[0].amiga_clock
        config, _ = complete_config(config, path, clock)
    return config


def complete_config(config: ConversionConfig, config_path: str | Path, amiga_clock: int,
                    song: SmpsSong | None = None) -> tuple[ConversionConfig, Derivation | None]:
    """A minimal config (no channels:) completed from its song, the ROM's DAC samples written to
    its samples_dir; any other config as it is."""
    if not config.is_minimal:
        return config, None
    song = song or config.read_song()
    derivation = derive_config(config.stated(), song, config_path, amiga_clock, read_dac(config.input_file))
    complete = ConversionConfig.from_data(derivation.data, str(config_path))
    samples = Path(complete.samples_dir)
    samples.mkdir(parents=True, exist_ok=True)
    for name, pcm in derivation.files.items():
        (samples / name).write_bytes(pcm)
    return complete, derivation


def derive_config(stated: dict, song: SmpsSong, config_path: str | Path, amiga_clock: int,
                  dac: list[DacSample] | None = None) -> Derivation:
    """`stated` (a config's YAML data) completed from `song`; `dac`: the ROM's DAC samples."""
    out = Derivation(dict(stated))
    _Deriver(stated, song, out, amiga_clock, dac or []).run(Path(config_path))
    return out


class _Deriver:
    def __init__(self, stated: dict, song: SmpsSong, out: Derivation, amiga_clock: int, dac: list[DacSample]):
        self._stated = stated
        self._song = song
        self._out = out
        self._clock = amiga_clock
        self._dac = {s.name: s for s in dac}
        self._next = 1                      # the next free instrument slot
        self._rows: list[list] = []         # sample_list rows derived
        self._levels: dict[tuple, Counter] = defaultdict(Counter)   # (kind, voice / label / form) -> level counts

    def run(self, config_path: Path) -> None:
        self._default("name", config_path.stem)
        self._default("output_file", _output_path(config_path))
        self._default("range_space", "chip")
        self._default("auto_bpm", True)

        sources = self._sources()
        self._default("channels", [{"source": s, "mod_channel": i} for i, s in enumerate(sources)])
        self._timing()

        fm, tone, noise, dac_names = self._notes(sources)
        self._dac_samples(dac_names)
        self._items("voice_map", self._windows(fm, "FM", lambda v: f"fm_v{v:02x}"))
        self._items("psg_voice_map", self._windows(tone, "tone", lambda label: f"psg_{label}"))
        self._items("psg_map", {form: self._noise(form) for form in sorted(noise)})
        self._sample_list()
        if self._next - 1 > MAX_INSTRUMENTS:
            raise ValueError(f"the derived config needs {self._next - 1} instruments, a MOD holds "
                             f"{MAX_INSTRUMENTS}: state voice_map entries that share a slot")

    # --- what the song plays ----------------------------------------------------------

    def _sources(self) -> list[str]:
        """The channels that sound, in header order."""
        return [name for name, ch in source_map(self._song).items()
                if any(e.note is not None and not e.note.is_rest for e in ch.events)]

    def _notes(self, sources: list[str]):
        """Each FM voice's and PSG envelope's chip pitches, the noise forms, the DAC samples hit."""
        fm: dict[int, Counter] = defaultdict(Counter)
        tone: dict[str, Counter] = defaultdict(Counter)
        noise: set[int] = set()
        dac: Counter = Counter()
        levels = self._levels
        bare = ConversionConfig()           # no maps: the walk tracks the driver's state only
        channels = source_map(self._song)
        for source in sources:
            ch = channels[source]
            for event, st, res in walk_channel(ch, bare, ChannelConfig(source=source, mod_channel=0)):
                note = event.note
                if note is None or note.is_rest:
                    continue
                if note.is_dac:
                    dac[note.dac_name] += 1
                elif res is not None and st.noise_form is not None:
                    noise.add(st.noise_form)
                    levels[("noise", st.noise_form)][(st.att, False)] += 1
                elif res is not None and st.is_psg:
                    tone[st.envelope or "$00"][res.chip] += 1
                    levels[("tone", st.envelope or "$00")][(st.att, False)] += 1
                elif res is not None and st.voice is not None:
                    fm[st.voice][res.chip] += 1
                    levels[("FM", st.voice)][(st.tl, st.hard_panned)] += 1
        return fm, tone, noise, dac

    # --- sections ---------------------------------------------------------------------

    def _timing(self) -> None:
        """Ticks per row: the grid every note starts and lasts on, coarsened until the song fits
        the pattern limit; the speed whose whole-number BPM is nearest the driver's tempo."""
        if "ticks_per_row" in self._stated:
            return
        ticks = [e.tick_position for ch in self._song.channels for e in ch.events if e.note is not None]
        ticks += [e.note.duration for ch in self._song.channels for e in ch.events if e.note is not None]
        grid = math.gcd(*ticks) or 1
        end = self._song.end_tick()
        limit = int(self._stated.get("max_patterns", 127))
        while end / grid / _ROWS_PER_PATTERN > limit:
            grid *= 2
        self._out.data["ticks_per_row"] = grid
        self._out.derived.append("ticks_per_row")

        h = self._song.header
        fps = 50 if str(self._stated.get("region", "ntsc")).lower() == "pal" else 60
        options = bpm_rounding_options(h.tempo_divider, h.tempo_modifier, grid, fps)
        self._default("target_speed", options[0]["speed"] if options else 6)

    def _windows(self, notes: dict, kind: str, file_stem) -> dict:
        """One entry per three-octave window of each voice's (envelope's) chip pitches."""
        out = {}
        for key in sorted(notes, key=str):
            entries = []
            for lo, hi in _windows(list(notes[key])):
                inst = self._take(kind, f"{file_stem(key)}_{_pitch_name(lo)}.raw", key)
                entries.append({"low": _pitch_name(lo), "high": _pitch_name(hi),
                                "mod_instrument": inst, "root": synth_note_name(_MOD_C1 + self._root_index(hi - lo))})
            out[key] = entries
        return out

    def _root_index(self, span: int) -> int:
        """The MOD note a window's lowest pitch plays at: the first whose rate clears the sample
        audit's low-rate line (E1 at the PAL clock), or as high as a wider window allows."""
        floor = next(i for i, period in enumerate(PERIOD_TABLE) if self._clock / period >= LOW_RATE_HZ)
        return min(floor, _MOD_SPAN - span)

    def _noise(self, form: int) -> dict:
        return {"mod_instrument": self._take("noise", f"psg_noise_{form:02x}.raw", form), "root": _NOISE_ROOT}

    def _dac_samples(self, hit: Counter) -> None:
        """A slot per DAC sample hit (a pitched copy shares its sample's): the sample at the note and
        finetune nearest its rate, a copy at the note nearest its own at that finetune; the PCM
        written to samples_dir."""
        if "dac_samples" in self._stated or not hit:
            return
        by_sound = {s.sound: s for s in self._dac.values()}
        slots: dict[int, int] = {}
        entries = []
        for name in sorted(hit):
            sample = self._dac.get(name)
            if sample is None:
                raise ValueError(f"DAC sample {name}: the ROM's driver has no such sample")
            base = by_sound[sample.of] if sample.of else sample
            if base.sound not in slots:
                file = f"{base.name}.raw"
                slots[base.sound] = self._take("DAC", file)
                self._rows[-1][_ROW_FINETUNE] = self._nearest(base.rate)[1]
                self._out.files[file] = base.pcm
            finetune = next(r[_ROW_FINETUNE] for r in self._rows if r[0] == slots[base.sound])
            note, _ = self._nearest(sample.rate, (finetune,))
            entries.append({"name": name, "mod_instrument": slots[base.sound], "mod_note": note})
        self._out.data["dac_samples"] = entries
        self._out.derived.append("dac_samples")
        self._default("samples_dir", (Path(self._out.data["output_file"]).parent / "samples").as_posix())

    def _nearest(self, rate: float, finetunes: Sequence[int] = _FINETUNES) -> tuple[str, int]:
        """The MOD note (C1-B3) and finetune a sample plays at `rate` on: the nearest in pitch (a
        finetune step is an eighth of a semitone)."""
        options = [(abs(math.log2(self._clock / period * 2 ** (ft / _FINETUNE_STEPS) / rate)), note, ft)
                   for note, period in enumerate(PERIOD_TABLE[:_MOD_SPAN + 1]) for ft in finetunes]
        _, note, ft = min(options)
        return ModNote(note).name, ft

    def _sample_list(self) -> None:
        """Derived rows for the derived slots; a stated row (by instrument) replaces its row."""
        stated = {row[0]: row for row in self._stated.get("sample_list", []) or []}
        rows = {row[0]: row for row in self._rows} | stated
        if self._rows:
            self._out.data["sample_list"] = [rows[i] for i in sorted(rows)]
            self._out.derived.append("sample_list")

    # --- helpers ------------------------------------------------------------------------

    def _take(self, kind: str, file: str, key=None) -> int:
        """The next slot, its sample_list row at the level its notes mostly play at."""
        counts = self._levels.get((kind, key))
        level, panned = max(counts, key=lambda lv: (counts[lv], -lv[0])) if counts else (0, False)
        inst = self._next
        self._next += 1
        self._rows.append([inst, file, starting_volume(kind, level, panned), 0])
        return inst

    def _default(self, key: str, value) -> None:
        if key not in self._stated:
            self._out.data[key] = value
            self._out.derived.append(key)

    def _items(self, section: str, derived: dict) -> None:
        """A map section: the derived items, each one a stated item names replaced."""
        if not derived and section not in self._stated:
            return
        stated = self._stated.get(section) or {}
        merged = {**derived, **stated}
        self._out.data[section] = merged
        if set(derived) - set(stated):
            self._out.derived.append(section)


def _pitch_name(semitone: int) -> str:
    """A chip pitch as the disassembly spells notes: F5, Bb2 (C0 = 0)."""
    return note_label(_FIRST_NOTE + semitone)[len("n"):]


def _windows(pitches: list[int]) -> list[tuple[int, int]]:
    """Distinct pitches cut into runs spanning at most a MOD's three octaves."""
    ps = sorted(set(pitches))
    out, start = [], 0
    while start < len(ps):
        end = start
        while end + 1 < len(ps) and ps[end + 1] - ps[start] <= _MOD_SPAN:
            end += 1
        out.append((ps[start], ps[end]))
        start = end + 1
    return out


def _output_path(config_path: Path) -> str:
    """configs/<sub>/<stem>.yaml -> output/<sub>/<stem>.mod (the configs tree mirrored)."""
    parts = list(config_path.with_suffix(".mod").parts)
    if "configs" in parts:
        parts[parts.index("configs")] = "output"
        return Path(*parts).as_posix()
    return (Path("output") / config_path.with_suffix(".mod").name).as_posix()
