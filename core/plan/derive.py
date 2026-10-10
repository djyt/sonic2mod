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
    FM / PSG tone   an entry per window of the chip pitches a voice / envelope plays: its lowest
                    on the first MOD note keeping samples.root_harmonics harmonics (never under
                    E1, the 5 kHz line), its top at most samples.top_note (A3), its span at most
                    samples.max_window.  The converter picks the rendering pitch
    FM drum         one per drum the drum track hits (Type 0 FM: core.smps.percussion), rendered
                    whole at samples.drum_root; nothing written
    DAC             one per sample; a pitched copy plays its sample's slot at the note nearest
                    its rate
    volumes         starting_volume: samples are peak-normalised, so the volume carries the level
                    the window's notes mostly play at, until vgm_compare --write-volumes measures
                    it.  An unmeasured window takes its voice's nearest measured correction
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from ..audio import db_to_gain, semitone_to_hz
from ..chips import DEFAULT_FM_PAN_LAW_DB, fm_level_db, psg_level_db
from ..config import (
    SAMPLE_FILE,
    SAMPLE_FINETUNE,
    SAMPLE_SLOT,
    SAMPLE_VOLUME,
    ChannelConfig,
    ConversionConfig,
    DacSample,
    SampleSettings,
    bpm_rounding_options,
    find_settings,
    load_settings,
    region_fps,
    variant_output_file,
)
from ..files import write_shared
from ..mod import LOW_RATE_HZ, PERIOD_TABLE, ModNote, note_rate, period_rate
from ..smps import (
    C1_SEMITONE,
    ChannelType,
    FmDrum,
    SmpsSong,
    note_label,
    pan_is_hard,
    psg_voice_name,
    source_map,
    synth_note_name,
)
from .driver_state import walk_channel

MAX_INSTRUMENTS = 31
_MOD_SPAN = 35               # C1 ... B3
_MIN_WINDOW = 12             # narrowest window root_harmonics may cut
_NO_ENVELOPE = psg_voice_name(0)     # "$00": a PSG note with no envelope
_NOISE_ROOT = "A3"           # a noise sample rendered at ~28 kHz keeps most of its hiss (Title Screen)
_ROWS_PER_PATTERN = 64
_MAX_GRID_STEPS = 32           # the coarsest row grid tried: 32 x the exact one
_WIDE_SPEEDS = range(2, 17)    # speeds tried where none of 2-8 gives a BPM within _BPM_CLOSE
_BPM_CLOSE = 0.1               # % off the driver's tempo
_FIRST_NOTE = 0x81           # the note byte of C0
_FINETUNES = range(-8, 8)    # a MOD sample's finetune
_FINETUNE_STEPS = 96         # finetune steps an octave

# Derived sample files.  A stated row naming one follows that sample to its slot
_FM_STEM = "fm_v{:02x}"                 # an FM voice's windows: fm_v04_C3.raw
_TONE_STEM = "psg_{}"                   # a PSG envelope's: psg_$00_Cs3.raw
_WINDOW_FILE = "{}_{}.raw"              # stem, lowest pitch
_NOISE_FILE = "psg_noise_{:02x}.raw"    # psg_noise_e7.raw
_NOISE_ENVELOPE_FILE = "psg_noise_{:02x}_{}.raw"     # psg_noise_e7_$00.raw: another envelope's
_DAC_FILE = "{}.raw"                    # the ROM's sample name: dac81.raw; an FM drum's: drum81.raw
_DERIVED_FILE = re.compile(r"(fm_v[0-9a-f]{2}_|psg_).*\.raw|(dac|drum)[0-9a-f]{2}\.raw")   # any of them

# Starting sample_list volumes at TL offset 0 / attenuation 0 (calibrated on Green Hill Zone)
_FM_SCALE = 76.0
_PSG_SCALE = 16.0
_FULL = 64


def starting_volume(kind: str, level: int = 0, hard_panned: bool = False,
                    pan_law_db: float = DEFAULT_FM_PAN_LAW_DB) -> int:
    """A sample_list volume to start from: a sample is peak-normalised, so the volume carries the
    level its notes mostly play at.  `kind` FM (level: TL offset), tone / noise (attenuation), DAC."""
    if kind == ChannelType.DAC:
        return _FULL
    if kind == ChannelType.FM:
        gain = _FM_SCALE * db_to_gain(fm_level_db(level, hard_panned, pan_law_db))
    else:
        gain = _PSG_SCALE * db_to_gain(psg_level_db(level))
    return max(1, min(_FULL, round(gain)))


class _Window(NamedTuple):
    voice: tuple    # (kind, voice / envelope)
    low: int        # lowest chip pitch


@dataclass
class Derivation:
    data: dict                                              # the config's YAML, stated and derived
    derived: list[str] = field(default_factory=list)        # the sections the song filled
    files: dict[str, bytes] = field(default_factory=dict)   # samples_dir files to write (DAC samples)
    stale: list[str] = field(default_factory=list)          # stated rows' files these settings do not
                                                            # cut (another max_window's, or stale): left out
    folded: dict[int, tuple[int, int]] = field(default_factory=dict)   # {voice copy: (the voice it plays
                                                            # as, its notes)}: no slot for it


def load_config(path: str | Path, settings_path: str | None = None, variant: str | None = None) -> ConversionConfig:
    """A config file read as `variant`, a minimal one completed from its song (what every tool
    reads a config with)."""
    config = ConversionConfig.from_yaml(str(path), variant)
    if config.is_minimal:
        settings = load_settings(settings_path or find_settings(str(path)))[0]
        config, _ = complete_config(config, path, settings)
    return config


def complete_config(config: ConversionConfig, config_path: str | Path, settings: SampleSettings,
                    song: SmpsSong | None = None) -> tuple[ConversionConfig, Derivation | None]:
    """A minimal config (no channels:) completed from its song, the ROM's DAC samples written to
    its samples_dir; any other config as it is."""
    if not config.is_minimal:
        return config, None
    song = song or config.read_song()
    derivation = derive_config(config.stated(), song, config_path, settings, config.read_dac())
    if config.variant is not None and "output_file" in derivation.derived:
        derivation.data["output_file"] = variant_output_file(derivation.data["output_file"], config.variant)
    complete = ConversionConfig.from_data(derivation.data, str(config_path), config.variant)
    samples = Path(complete.samples_dir)
    samples.mkdir(parents=True, exist_ok=True)
    for name, pcm in derivation.files.items():
        write_shared(samples / name, pcm)       # other conversions may be reading it
    return complete, derivation


def derive_config(stated: dict, song: SmpsSong, config_path: str | Path, settings: SampleSettings,
                  dac: list[DacSample] | None = None) -> Derivation:
    """`stated` (a config's YAML data) completed from `song`; `settings`: the clock and window
    placement; `dac`: the ROM's DAC samples."""
    out = Derivation(dict(stated))
    _Deriver(stated, song, out, settings, dac or []).run(Path(config_path))
    return out


class _Deriver:
    def __init__(self, stated: dict, song: SmpsSong, out: Derivation, settings: SampleSettings,
                 dac: list[DacSample]):
        self._stated = stated
        self._song = song
        self._out = out
        self._clock = settings.amiga_clock
        self._harmonics = settings.root_harmonics
        self._top = settings.top_note
        self._max_window = settings.max_window
        self._drum_root = ModNote(settings.drum_root).name
        # Lowest MOD note a window starts at: the first above the sample audit's low-rate line
        self._floor = next(i for i, period in enumerate(PERIOD_TABLE) if period_rate(period, self._clock) >= LOW_RATE_HZ)
        self._dac = {s.name: s for s in dac}
        self._next = 1                      # the next free instrument slot
        self._rows: list[list] = []         # sample_list rows derived
        # (kind, voice / label / form) -> {(chip pitch, level): notes}; noise and DAC: pitch None
        self._levels: dict[tuple, Counter] = defaultdict(Counter)
        self._group: dict[int, _Window] = {}  # slot -> the voice window it holds

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
        fm = self._fold_copies(fm, MAX_INSTRUMENTS - (self._next - 1) - self._psg_slots(tone, noise))
        voice_map = self._windows(fm, ChannelType.FM, _FM_STEM.format)
        self._items("voice_map", voice_map | {copy: voice_map[plain] for copy, (plain, _) in self._out.folded.items()})
        self._items("psg_voice_map", self._windows(tone, "tone", _TONE_STEM.format))
        self._items("psg_map", {form: self._noise(form, noise[form]) for form in sorted(noise)})
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
        """Each FM voice's and PSG envelope's chip pitches, each noise form's envelopes, the DAC
        samples hit."""
        fm: dict[int, Counter] = defaultdict(Counter)
        tone: dict[str, Counter] = defaultdict(Counter)
        noise: dict[int, Counter] = defaultdict(Counter)
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
                    label = st.envelope or _NO_ENVELOPE
                    noise[st.noise_form][label] += 1
                    levels[("noise", (st.noise_form, label))][(None, (st.att, False))] += 1
                elif res is not None and st.is_psg:
                    tone[st.envelope or _NO_ENVELOPE][res.chip] += 1
                    levels[("tone", st.envelope or _NO_ENVELOPE)][(res.chip, (st.att, False))] += 1
                elif res is not None and st.voice is not None:
                    fm[st.voice][res.chip] += 1
                    levels[(ChannelType.FM, st.voice)][(res.chip, (st.tl, st.hard_panned))] += 1
        return fm, tone, noise, dac

    # --- sections ---------------------------------------------------------------------

    def _timing(self) -> None:
        """Ticks per row: the grid every note starts and lasts on.  Where that grid is too fine (the
        pattern limit, or no speed's BPM in 32-255), the multiple of it that puts the most notes on
        rows: the rest take EDx.  A 1-frame stagger between channels makes the exact grid 1 frame;
        the beat's grid (7 frames) keeps all but the staggered notes on rows.  Where it is too
        coarse (no speed's BPM reaches 32: Space Harrier II's Game Over, three long notes), the
        largest part of it that fits.  The speed whose whole-number BPM is nearest the driver's tempo."""
        if "ticks_per_row" in self._stated:
            return
        # The song's own rhythm: where a driver cuts a note (a run-out, a gate) falls between rows
        # (ECx); the note's onset is the song's
        notes = [(e.tick_position, e.note) for ch in self._song.channels for e in ch.events
                 if e.note is not None and not (e.note.cut and e.note.is_rest)]
        ticks = [tick for tick, _ in notes] + [note.duration for _, note in notes if not note.cut]
        exact = math.gcd(*ticks) or 1
        starts = Counter(tick for tick, _ in notes)

        def on_rows(grid: int) -> int:
            return sum(n for tick, n in starts.items() if tick % grid == 0)

        fitting = [g for g in range(exact, _MAX_GRID_STEPS * exact + 1, exact) if self._grid_options(g) is not None]
        if not fitting:
            fitting = self._refined(exact)
        grid = max(fitting, key=lambda g: (on_rows(g), -g)) if fitting else self._coarsened(exact)
        tpr, options = self._grid_options(grid) or self._tpr_options(grid)
        self._out.data["ticks_per_row"] = tpr
        self._out.derived.append("ticks_per_row")

        self._default("target_speed", options[0]["speed"] if options else 6)

    def _tpr_options(self, grid: int) -> tuple[float, list[dict]]:
        """Ticks per row for `grid` and the speeds whose BPM fits.  Stored ticks hold the header's
        tempo divider; ticks_per_row counts duration units (Timeline multiplies the divider back in).
        Speeds 2-8, or up to 16 where none of those comes close: Good Ending's 13-frame rows are
        150 BPM at speed 13, 80.77 at 7."""
        h = self._song.header
        divider = max(h.tempo_divider, 1)
        tpr = grid // divider if grid % divider == 0 else grid / divider
        fps = region_fps(self._stated.get("region", "ntsc"))
        options = bpm_rounding_options(h.tempo_divider, h.tempo_modifier, tpr, fps)
        if not options or abs(options[0]["error_pct"]) > _BPM_CLOSE:
            options = bpm_rounding_options(h.tempo_divider, h.tempo_modifier, tpr, fps, speeds=_WIDE_SPEEDS) or options
        return tpr, options

    def _grid_options(self, grid: int) -> tuple[float, list[dict]] | None:
        """`grid`'s ticks per row and speeds, if the song fits the pattern limit at it and some speed's
        BPM fits (a tempo without one: any); else None."""
        limit = int(self._stated.get("max_patterns", 127))
        if self._song.end_tick() / grid / _ROWS_PER_PATTERN > limit:
            return None
        tpr, options = self._tpr_options(grid)
        return (tpr, options) if options or self._song.header.tempo_modifier <= 1 else None

    def _refined(self, grid: int) -> list[int]:
        """The largest part of `grid` (a divisor) at which some speed's BPM fits, where `grid` is
        too coarse for any: [] if none (or `grid` is not too coarse)."""
        if self._tpr_options(grid)[1]:
            return []
        return next(([g] for g in range(grid // 2, 0, -1) if grid % g == 0 and self._grid_options(g) is not None), [])

    def _coarsened(self, grid: int) -> int:
        """`grid` doubled until it fits, or reaches the song's end."""
        end = self._song.end_tick()
        while grid < end and self._grid_options(grid) is None:
            grid *= 2
        return grid

    def _windows(self, notes: dict, kind: str, file_stem) -> dict:
        """An entry per window of each voice's (envelope's) chip pitches."""
        out = {}
        for key in sorted(notes, key=str):
            entries = []
            for lo, hi in _windows(list(notes[key]), self._max_span):
                inst = self._take(kind, _WINDOW_FILE.format(file_stem(key), _pitch_name(lo)), key, (lo, hi))
                root = min(self._lowest_root(lo), self._top - (hi - lo))
                entries.append({"low": _pitch_name(lo), "high": _pitch_name(hi),
                                "mod_instrument": inst, "root": synth_note_name(C1_SEMITONE + root)})
            out[key] = entries
        return out

    def _fold_copies(self, fm: dict[int, Counter], slots: int) -> dict[int, Counter]:
        """`fm` with copies folded onto their voices while its windows outnumber `slots`: the least
        played copy (channel 3 special mode, the LFO: SmpsVoice.plain) plays as its voice, its notes
        in that voice's windows (Derivation.folded)."""
        voices = {v.index: v for v in self._song.voices}
        fm = {key: Counter(pitches) for key, pitches in fm.items()}
        while self._fm_slots(fm) > slots:
            copies = [key for key in fm if voices[key].plain is not None]
            if not copies:
                break
            copy = min(copies, key=lambda key: (fm[key].total(), key))
            plain = voices[copy].plain
            assert plain is not None
            self._out.folded[copy] = (plain, fm[copy].total())
            fm.setdefault(plain, Counter()).update(fm.pop(copy))       # a plain voice is no copy: never folded
        return fm

    def _fm_slots(self, fm: dict[int, Counter]) -> int:
        return sum(len(_windows(list(pitches), self._max_span)) for pitches in fm.values())

    def _psg_slots(self, tone: dict[str, Counter], noise: dict[int, Counter]) -> int:
        """The slots the PSG's windows and noise forms take (each envelope a noise form plays with)."""
        return sum(len(_windows(list(pitches), self._max_span)) for pitches in tone.values()) + \
            sum(len(envelopes) for envelopes in noise.values())

    def _lowest_root(self, pitch: int) -> int:
        """First MOD note (0 = C1) from the floor keeping root_harmonics harmonics of `pitch` below
        Nyquist; past top_note when none does."""
        need = 2 * self._harmonics * semitone_to_hz(pitch)
        return next((i for i in range(self._floor, self._top + 1) if note_rate(i, self._clock) >= need),
                    self._top + 1)

    def _max_span(self, pitch: int) -> int:
        """Widest window from `pitch`: up to top_note from the note `pitch` needs (an octave at
        least), at most max_window."""
        span = min(self._top - self._floor, max(_MIN_WINDOW, self._top - self._lowest_root(pitch)))
        return min(span, self._max_window) if self._max_window else span

    def _noise(self, form: int, envelopes: Counter) -> dict:
        """The form's slot, rendered with the envelope most of its notes play (the converter's
        vote: derive_noise_envelopes), and a slot for each other envelope it plays with."""
        own = max(envelopes, key=lambda label: envelopes[label])
        entry = {"mod_instrument": self._take("noise", _NOISE_FILE.format(form), (form, own)), "root": _NOISE_ROOT}
        others = {label: self._take("noise", _NOISE_ENVELOPE_FILE.format(form, label), (form, label))
                  for label in envelopes if label != own}
        return entry | ({"envelopes": others} if others else {})

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
            drum = self._song.fm_drums.get(name)
            if drum is not None:
                if not drum.silent:          # a silent drum's hits are cuts: no sample
                    entries.append(self._fm_drum(name, drum))
                continue
            sample = self._dac.get(name)
            if sample is None:
                raise ValueError(f"DAC sample {name}: the ROM's driver has no such sample")
            base = by_sound[sample.of] if sample.of else sample
            if base.sound not in slots:
                file = _DAC_FILE.format(base.name)
                slots[base.sound] = self._take(ChannelType.DAC, file)
                self._rows[-1][SAMPLE_FINETUNE] = self._nearest(base.rate)[1]
                self._out.files[file] = base.pcm
            finetune = next(r[SAMPLE_FINETUNE] for r in self._rows if r[SAMPLE_SLOT] == slots[base.sound])
            note, _ = self._nearest(sample.rate, (finetune,))
            entries.append({"name": name, "mod_instrument": slots[base.sound], "mod_note": note})
        self._out.data["dac_samples"] = entries
        self._out.derived.append("dac_samples")
        self._default("samples_dir", (Path(self._out.data["output_file"]).parent / "samples").as_posix())

    def _fm_drum(self, name: str, drum: FmDrum) -> dict:
        """An FM drum's slot, rendered at samples.drum_root: its volume the FM level law's at the
        drum's own volume (its render is peak-normalised, as an FM voice's)."""
        hard = drum.voice.pan is not None and pan_is_hard(drum.voice.pan)
        slot = self._take(ChannelType.FM, _DAC_FILE.format(name),
                          volume=starting_volume(ChannelType.FM, drum.tl_offset, hard))
        return {"name": name, "mod_instrument": slot, "mod_note": self._drum_root}

    def _nearest(self, rate: float, finetunes: Sequence[int] = _FINETUNES) -> tuple[str, int]:
        """The MOD note (C1-B3) and finetune a sample plays at `rate` on: the nearest in pitch (a
        finetune step is an eighth of a semitone)."""
        options = [(abs(math.log2(period_rate(period, self._clock) * 2 ** (ft / _FINETUNE_STEPS) / rate)), note, ft)
                   for note, period in enumerate(PERIOD_TABLE[:_MOD_SPAN + 1]) for ft in finetunes]
        _, note, ft = min(options)
        return ModNote(note).name, ft

    def _sample_list(self) -> None:
        """Derived rows, stated ones over them.  A stated row naming a derived file follows it to its
        slot (settings renumber slots); one naming a file these settings do not cut is left out; any
        other replaces its slot's row (the user's own sample)."""
        slot_of = {row[SAMPLE_FILE]: row[SAMPLE_SLOT] for row in self._rows}
        stated = {}
        for row in self._stated.get("sample_list", []) or []:
            file = row[SAMPLE_FILE]
            if file in slot_of:
                stated[slot_of[file]] = [slot_of[file], *row[SAMPLE_FILE:]]
            elif _DERIVED_FILE.fullmatch(str(file)):
                self._out.stale.append(file)
            else:
                stated[row[SAMPLE_SLOT]] = row
        rows = {row[SAMPLE_SLOT]: row for row in self._rows} | self._inherited(stated) | stated
        if self._rows:
            self._out.data["sample_list"] = [rows[i] for i in sorted(rows)]
            self._out.derived.append("sample_list")

    def _inherited(self, stated: dict[int, list]) -> dict[int, list]:
        """Unmeasured windows' rows: the starting volume times the nearest measured window's
        correction (measured / starting).  Beyond the level, a correction is the voice's."""
        derived = {row[SAMPLE_SLOT]: row for row in self._rows}
        measured = [slot for slot in stated
                    if slot in self._group and stated[slot][SAMPLE_FILE] == derived[slot][SAMPLE_FILE]]
        out = {}
        for slot, window in self._group.items():
            if slot in stated:
                continue
            siblings = [m for m in measured if self._group[m].voice == window.voice]
            if not siblings:
                continue
            near = min(siblings, key=lambda m: abs(self._group[m].low - window.low))
            ratio = float(stated[near][SAMPLE_VOLUME]) / max(derived[near][SAMPLE_VOLUME], 1)
            row = list(derived[slot])
            row[SAMPLE_VOLUME] = max(1, min(_FULL, round(row[SAMPLE_VOLUME] * ratio)))
            out[slot] = row
        return out

    # --- helpers ------------------------------------------------------------------------

    def _take(self, kind: str, file: str, key=None, window: tuple[int, int] | None = None,
              volume: int | None = None) -> int:
        """The next slot, its row at the level its notes (`window`'s, lowest and highest pitch)
        mostly play at, or at `volume`."""
        counts = Counter()
        for (pitch, lv), n in self._levels.get((kind, key), Counter()).items():
            if window is None or window[0] <= pitch <= window[1]:
                counts[lv] += n
        level, panned = max(counts, key=lambda lv: (counts[lv], -lv[0])) if counts else (0, False)
        inst = self._next
        self._next += 1
        if window is not None:
            self._group[inst] = _Window((kind, key), window[0])
        self._rows.append([inst, file, starting_volume(kind, level, panned) if volume is None else volume, 0])
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


def _windows(pitches: list[int], max_span) -> list[tuple[int, int]]:
    """Distinct pitches cut into runs, each spanning at most `max_span(its lowest pitch)`."""
    ps = sorted(set(pitches))
    out, start = [], 0
    while start < len(ps):
        end, span = start, max_span(ps[start])
        while end + 1 < len(ps) and ps[end + 1] - ps[start] <= span:
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
