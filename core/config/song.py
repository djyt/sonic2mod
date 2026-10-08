"""A song's conversion config (ConversionConfig): what convert.py reads from configs/<song>.yaml."""

import os
from dataclasses import dataclass, field
from typing import Any

from ..mod import ModFile
from ..smps import DEFAULT_DRIVER, SmpsDriver, SmpsSong
from ..source import LiftOptions, is_rom_path, is_vgm_path, read_song
from .entries import (
    TWIN_MODES,
    parse_channel_instrument_map,
    parse_channels,
    parse_dac_samples,
    parse_merge_groups,
    parse_pattern_breaks,
    parse_psg_map,
    parse_psg_voice_map,
    parse_voice_maps,
)
from .loader import VARIANTS_KEY, apply_variant, read_yaml_file

# A song config's top-level keys
_KEYS = frozenset({
    "name", "input_file", "rom_song", "driver", "tempo_modifier", "tempo_divider", "output_file", "target_bpm", "target_speed", "ticks_per_row", "num_mod_channels",
    "auto_bpm", "region", "range_space", "samples_dir", "max_patterns", "channels", "dac_samples", "voice_map",
    "channel_instrument_map", "psg_map", "psg_voice_map", "sample_list", "mod_pattern_breaks", "merge",
    "merge_patterns", "merge_drop", "merge_fill", "merge_fill_cut_after", "merge_output_file",
    "merge_max_synth_shift", "merge_tolerance", "merge_bank_slots", "merge_twins", "merge_loop_timbre", "loop_drift_db",
    "treble_shelf_db",
})

_TEMPO_OVERRIDES = ("tempo_modifier", "tempo_divider")

# Console region (`region:`) -> V-int frames a second: what the tempo, note fills and modulation count in
REGION_FPS = {"ntsc": 60, "pal": 50}


def region_fps(region: str) -> int:
    """V-int frames a second for a `region:` value (ntsc | pal, any case)."""
    fps = REGION_FPS.get(str(region).lower())
    if fps is None:
        raise ValueError(f"region must be one of {', '.join(REGION_FPS)} (got {region!r})")
    return fps


def _region(data: dict, filepath) -> str:
    """`region:`, lower-cased; an unknown one is an error."""
    region = str(data.get("region", "ntsc")).lower()
    if region not in REGION_FPS:
        raise ValueError(f"{filepath}: region must be one of {', '.join(REGION_FPS)} (got {data['region']!r})")
    return region


@dataclass
class ConversionConfig:
    name: str = "Untitled"
    input_file: str = ""
    rom_song: int | None = None   # a ROM input_file: the sound ID to convert ($81 ...)
    # The `variants:` entry the config was read as (convert.py --variant; None: the base build)
    variant: str | None = None
    _data: dict = field(default_factory=dict, repr=False)   # the YAML it was read from
    # The SMPS variant that played the song; None: not stated (a ROM's is detected, an asm or a
    # VGM log is Sonic 1's).  An asm input states its tempo itself; a VGM / VGZ one is lifted
    # (core/vgm/lift/), and these override the tempo the lift infers.
    driver: SmpsDriver | None = None
    tempo_modifier: int | None = None
    tempo_divider: int | None = None
    output_file: str = "output.mod"
    target_bpm: int = 150
    target_speed: int = 6
    ticks_per_row: float = 6.0
    # MOD channel count.  None (the default) derives it from the channels section: the highest
    # mod_channel + 1, rounded up to a count a format tag exists for (4, 8, 10, 12, 14, 16).
    # Set it only to pad upward, so a spare channel can carry Fxx / Dxx when no note cell has a
    # free effect slot.
    num_mod_channels: int | None = None
    auto_bpm: bool = False        # Derive BPM from SMPS tempo header
    region: str = "ntsc"          # "ntsc" (60 Hz) or "pal" (50 Hz)
    # What voice_map / psg_voice_map low/high (and root's anchor) are compared with:
    #   "source" - the SMPS note byte (default; what every config before Credits uses)
    #   "chip"   - the real pitch the chip plays: byte + pitch_offset + smpsChangeTransposition
    #              (PSG: + 3 octaves, the table's nC0 being C3).  Needed when a song changes key
    #              with $E9 while keeping a voice: the same byte must then reach different notes.
    range_space: str = "source"
    channels: list = field(default_factory=list)       # list of ChannelConfig
    dac_samples: list = field(default_factory=list)    # list of DacSampleConfig

    sample_list: list | None = None                 # [inst_num, filename, volume, finetune]
    samples_dir: str = "./samples/"
    max_patterns: int = 127
    voice_map: dict = field(default_factory=dict)         # {voice_index: list[InstrumentRange]}
    channel_instrument_map: dict = field(default_factory=dict)  # {source_channel: {voice_index: list[InstrumentRange]}}
    psg_map: dict = field(default_factory=dict)           # {form_byte_int: PsgInstrumentEntry}; type auto-inferred from bit 2
    psg_voice_map: dict = field(default_factory=dict)     # {"fTone_01": list[PsgInstrumentEntry], ...}
    mod_pattern_breaks: list = field(default_factory=list)  # [(pattern_slot, row), ...] — insert Bxx + split pattern
    # Channel folding for the reduced (Amiga) build, used with `convert.py --merged` (core/merge/):
    # each group's followers are dropped and their notes rendered into the primary's instruments.
    merge: list = field(default_factory=list)              # list[MergeGroup]; a `merge_patterns:` group
                                                           # carries its patterns, a `merge:` one is song-wide
    merge_drop: list = field(default_factory=list)         # channels left out of the merged build altogether
    # `merge_patterns:` (folds that differ per pattern, tools/fold_csv.py writes it from a fold table):
    # every pattern number the blocks name, and {channel: patterns} its notes are dropped in
    merge_patterns_named: set = field(default_factory=set)
    merge_pattern_drop: dict = field(default_factory=dict)
    # Channels whose notes go to the fill pool: each note is placed on whichever output channel is
    # silent at that moment (core/merge/ pool_notes), or lost where none is
    merge_fill: list = field(default_factory=list)
    # {output channel: ticks}: after that many ticks of one of its notes the channel counts as silent
    # for the fill pool, so a pool note cuts the note's tail (a chime over a kick's decay, over a bass
    # note's second half); a channel absent here is never cut.  Least 1.
    merge_fill_cut_after: dict = field(default_factory=dict)
    # Ticks a follower's note-on may be from the primary's and still fold (Green Hill's FM3 starts
    # its chord one tick after FM4/FM5); a note that short before a legato note is a grace note,
    # folded to the note it bends into.
    merge_tolerance: int = 1
    # Instrument slots the composite fit leaves free for the sample banks of the `bank: true`
    # groups (core/merge/banks.py); the banks take any other slot still free after the fit as well.
    # "auto" (the default): the converter chooses, rebuilding once the banks' sizes are known
    # (SmpsToModConverter.convert); a number pins it.  `merge_bank_slots` is the count in use.
    merge_bank_slots: int = 2
    merge_bank_slots_auto: bool = True
    # When a composite whose shape a surviving one has gives up its slot (core.merge._twins):
    # "short" only while the composites do not all fit; "always" in any case, for the bytes
    merge_twins: str = "short"
    # Song-level override of settings.yaml loop_drift_db (dB a loop may freeze above the settled
    # level): a lofi build lets loops freeze early for shorter samples
    loop_drift_db: float | None = None
    # Song-level override of settings.yaml treble_shelf_db (a brightness shelf; None: the settings')
    treble_shelf_db: float | None = None
    # Merged build only: cap on the semitones a sample is rendered above the pitch its root sounds
    # (resolve_synth_roots; 12 = the usual octave).  0 halves every shifted sample's bytes and rate.
    merge_max_synth_shift: int = 12
    # The merged build's sustain loops wait for the timbre to hold too (core.audio.loops
    # PROFILE_PER_DB), as the reference build's always do; off by default, as it grows the samples
    merge_loop_timbre: bool = False
    merge_output_file: str | None = None                   # default: output_file stem + "_merged"
    merge_active: bool = False                             # set by core.merge.prepare_merged_config
    merge_plan: Any = field(default=None, repr=False)      # core.merge.MergePlan, set by the converter
    detune_plan: Any = field(default=None, repr=False)     # core.plan.detune.DetunePlan, set by the converter

    @property
    def mod_channel_count(self) -> int:
        """The MOD's channel count: `num_mod_channels` when set, else derived from the channels."""
        if self.num_mod_channels is not None:
            return self.num_mod_channels
        highest = max((c.mod_channel for c in self.channels if c.enabled), default=-1)
        return ModFile.round_up_channels(highest + 1)

    @property
    def lift_options(self) -> LiftOptions:
        return LiftOptions(self.driver or DEFAULT_DRIVER, self.tempo_modifier, self.tempo_divider)

    def read_song(self) -> SmpsSong:
        """The song `input_file` holds: assembly parsed, a ROM's song decoded, a VGM / VGZ rip lifted."""
        return read_song(self.input_file, self.lift_options, self.rom_song, self.driver)

    def validate_mod_channels(self) -> None:
        """Reject a `num_mod_channels` no format tag exists for, or one the channels overflow."""
        n = self.num_mod_channels
        if n is None:
            return
        valid = list(ModFile.valid_channel_counts())
        if n not in valid:
            raise ValueError(f"num_mod_channels must be one of {valid} (got {n})")
        over = sorted(c.mod_channel for c in self.channels if c.enabled and c.mod_channel >= n)
        if over:
            raise ValueError(
                f"num_mod_channels: {n} leaves no room for mod_channel {over[0]} "
                f"(needs at least {ModFile.round_up_channels(over[-1] + 1)})"
            )

    @classmethod
    def from_yaml(cls, filepath, variant: str | None = None):
        """Load configuration from a YAML file, read as `variant` (core.config.loader.apply_variant);
        a key it does not know is an error (a typo, or a retired key, would otherwise be ignored).
        A variant that states no output_file writes <output_file stem>_<variant>.mod."""
        data = read_yaml_file(filepath)
        resolved = apply_variant(data, variant, str(filepath))
        if variant is not None and "output_file" not in ((data.get(VARIANTS_KEY) or {}).get(variant) or {}):
            stem, ext = os.path.splitext(resolved.get("output_file", "output.mod"))
            resolved["output_file"] = f"{stem}_{variant}{ext}"
        return cls.from_data(resolved, filepath, variant)

    @classmethod
    def from_data(cls, data: dict, filepath, variant: str | None = None) -> "ConversionConfig":
        """A config from its YAML data with its variant applied (a file's, or a minimal one completed
        by core.plan.derive_config)."""
        unknown = sorted(set(data) - _KEYS)
        if unknown:
            raise ValueError(f"{filepath}: unknown key(s): {', '.join(unknown)}")
        config = cls(
            name=data.get('name', 'Untitled'),
            variant=variant,
            input_file=data.get('input_file', ''),
            output_file=data.get('output_file', 'output.mod'),
            target_bpm=data.get('target_bpm', 150),
            target_speed=data.get('target_speed', 6),
            ticks_per_row=data.get('ticks_per_row', 6.0),
            num_mod_channels=data.get('num_mod_channels'),
            auto_bpm=data.get('auto_bpm', False),
            region=_region(data, filepath),
            range_space=str(data.get('range_space', 'source')),
            samples_dir=data.get('samples_dir', './samples/'),
            max_patterns=data.get('max_patterns', 127),
        )
        config.channels = parse_channels(data)
        config.dac_samples = parse_dac_samples(data)
        config.voice_map = parse_voice_maps(data)
        config.channel_instrument_map = parse_channel_instrument_map(data)
        config.psg_map = parse_psg_map(data, filepath)
        config.psg_voice_map = parse_psg_voice_map(data)
        config.sample_list = data.get('sample_list')
        config._read_merge(data)
        config.mod_pattern_breaks = parse_pattern_breaks(data)
        config.validate_mod_channels()
        config._read_source(data)
        config._data = dict(data)
        return config

    @property
    def fps(self) -> int:
        """V-int frames a second of the config's region."""
        return region_fps(self.region)

    @property
    def is_minimal(self) -> bool:
        """No `channels:` section: everything the config leaves out is derived from the song."""
        return "channels" not in self._data

    def stated(self) -> dict:
        """The YAML data the config states, with any later input_file / rom_song override."""
        data = dict(self._data)
        data["input_file"] = self.input_file
        if self.rom_song is not None:
            data["rom_song"] = f"${self.rom_song:02X}"
        return data

    def use_source(self, input_file: str, rom_song: int | str | None = None) -> None:
        """Convert `input_file` (a ROM: its sound `rom_song`, 129 / "$81" / "0x81")."""
        if rom_song is not None and not is_rom_path(input_file):
            raise ValueError("rom_song: applies to a ROM input_file only (.bin / .md / .gen)")
        self.input_file = input_file
        self.rom_song = None if rom_song is None else _sound_id(rom_song)

    def _read_source(self, data: dict) -> None:
        """driver:, rom_song: (a ROM input only) and the tempo overrides (a VGM input only)."""
        self.use_source(self.input_file, data.get('rom_song'))

        name = data.get('driver')
        if name is not None:
            if str(name) not in SmpsDriver:
                raise ValueError(f"driver: {name!r} is not one of {', '.join(SmpsDriver)}")
            self.driver = SmpsDriver(str(name))

        for key in _TEMPO_OVERRIDES:
            value = data.get(key)
            if value is None:
                continue
            if not is_vgm_path(self.input_file):
                raise ValueError(f"{key}: applies to a .vgm / .vgz input_file only (the asm states its tempo)")
            if int(value) < 1:
                raise ValueError(f"{key}: must be at least 1 (got {value})")
            setattr(self, key, int(value))

    def _read_merge(self, data: dict) -> None:
        """The merged build's settings: the groups, the dropped and pooled channels, the slot
        budgets, the song's overrides of settings.yaml."""
        self.merge, self.merge_patterns_named, self.merge_pattern_drop = parse_merge_groups(data)
        drop = data.get('merge_drop', []) or []
        self.merge_drop = [str(d) for d in ([drop] if isinstance(drop, str) else drop)]
        fill = data.get('merge_fill', []) or []
        self.merge_fill = [str(d) for d in ([fill] if isinstance(fill, str) else fill)]
        cut_after = data.get('merge_fill_cut_after', {}) or {}
        if not isinstance(cut_after, dict):
            raise ValueError("merge_fill_cut_after must be a mapping of channel to ticks")
        self.merge_fill_cut_after = {str(k): max(1, int(v)) for k, v in cut_after.items()}
        self.merge_output_file = data.get('merge_output_file')
        self.merge_max_synth_shift = int(data.get('merge_max_synth_shift', 12))
        self.merge_loop_timbre = bool(data.get('merge_loop_timbre', False))
        self.merge_tolerance = int(data.get('merge_tolerance', 1))
        bank_slots = data.get('merge_bank_slots', 'auto')
        self.merge_bank_slots_auto = str(bank_slots).lower() == 'auto'
        if not self.merge_bank_slots_auto:
            self.merge_bank_slots = max(0, int(bank_slots))
        self.merge_twins = str(data.get('merge_twins', 'short'))
        if self.merge_twins not in TWIN_MODES:
            raise ValueError(f"merge_twins: {self.merge_twins!r} is not one of {', '.join(TWIN_MODES)}")
        if data.get('loop_drift_db') is not None:
            self.loop_drift_db = max(0.0, float(data['loop_drift_db']))
        if data.get('treble_shelf_db') is not None:
            self.treble_shelf_db = float(data['treble_shelf_db'])


def _sound_id(value) -> int:
    """rom_song: as YAML gives it: 129, "$81" or "0x81"."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    for prefix in ("$", "0x"):
        if text.startswith(prefix):
            return int(text[len(prefix):], 16)
    raise ValueError(f"rom_song: {value!r} is not a sound ID ($81 ...)")
