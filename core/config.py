"""Per-song conversion configuration."""

import warnings
from dataclasses import dataclass, field
from typing import Optional

from .tables import ModNote, parse_smps_note


@dataclass
class InstrumentRange:
    low: int            # inclusive lower bound — SMPS semitone from C0 (e.g. nA2 = 33)
    high: int           # inclusive upper bound — SMPS semitone from C0
    mod_instrument: int # MOD instrument number (1-31)
    root: Optional[ModNote] = None  # MOD note where `low` plays;
                                    # out_note = root + (source_semitone - low)
                                    # if None: fall back to channel transpose for note
    synth_root: Optional[int] = None  # SMPS semitone to synthesize at (None = use low)
                                      # target_rate is NOT adjusted — output pitch equals
                                      # synth_root's frequency when played at root's period


def _parse_instrument_range(entry: dict) -> "InstrumentRange":
    """Parse a single InstrumentRange dict from YAML.

    Accepts both new key ``mod_instrument`` and deprecated ``instrument``
    (emits DeprecationWarning for the latter).
    """
    low  = parse_smps_note(entry['low'])
    high = parse_smps_note(entry['high'])

    if 'mod_instrument' in entry:
        inst = entry['mod_instrument']
    elif 'instrument' in entry:
        warnings.warn(
            "YAML key 'instrument' in a range entry is deprecated; use 'mod_instrument'.",
            DeprecationWarning,
            stacklevel=4,
        )
        inst = entry['instrument']
    else:
        raise KeyError(f"InstrumentRange entry missing 'mod_instrument': {entry}")

    root       = ModNote[entry['root']]              if 'root'       in entry else None
    synth_root = parse_smps_note(entry['synth_root']) if 'synth_root' in entry else None

    return InstrumentRange(
        low=low, high=high, mod_instrument=inst, root=root, synth_root=synth_root
    )


@dataclass
class ChannelConfig:
    source: str          # "DAC", "FM1"-"FM5", "PSG1"-"PSG3"
    mod_channel: int     # 0-based MOD channel index
    transpose: int = 0   # Semitone offset
    instrument: int = 1  # MOD instrument number (1-31)
    volume: int = 64     # MOD volume (0-64)
    enabled: bool = True


@dataclass
class DacSampleConfig:
    name: str            # e.g. "dKick"
    mod_instrument: int  # MOD instrument number
    mod_note: str = "C3" # Note to trigger in MOD


@dataclass
class PsgInstrumentEntry:
    mod_instrument: int                      # MOD slot (1-based)
    type: str                                # "tone" | "white_noise" | "periodic_noise"
    root: 'ModNote'                          # MOD note anchor; determines target_rate + WHERE sample triggers
    synth_root: Optional[int] = None        # Synthesis pitch override — SMPS semitone (None = use root)
    low: Optional[int] = None               # SMPS semitone anchor for melodic root offset (tone entries)
    noise_rate: int = 0                      # Only for noise types: 0, 1, 2 (preset dividers)
    envelope: object = None                  # Named table str ("PSG4") or inline list[int]; None = constant volume
    base_volume: int = 0                     # SN76489 base attenuation (0=max, 15=silent)


@dataclass
class PsgSynthesisSettings:
    enabled: bool = False
    clock_rate: int = 3_579_545      # SN76489 NTSC MD clock (Hz)
    amiga_clock: int = 3_546_895     # PAL Amiga clock for target_rate calculation
    sustain_duration: float = 1.0
    release_padding: float = 0.2
    normalize_samples: bool = False  # True = per-sample normalize; False = global (preserves balance)
    psg_output_max: int = 4096       # Hardware PSG max amplitude; noise peaks at 4096/2=2048 (C source halves it)
    psg_envelope_tables: dict = field(default_factory=dict)  # {"PSG1": [0,0,...], ...}

    @classmethod
    def from_yaml(cls, filepath: str) -> 'PsgSynthesisSettings':
        import yaml
        with open(filepath) as f:
            data = yaml.safe_load(f)
        s = data.get("psg_synthesis", {})
        return cls(
            enabled=s.get("enabled", False),
            clock_rate=s.get("clock_rate", 3_579_545),
            amiga_clock=s.get("amiga_clock", 3_546_895),
            sustain_duration=s.get("sustain_duration", 1.0),
            release_padding=s.get("release_padding", 0.2),
            normalize_samples=s.get("normalize_samples", False),
            psg_output_max=s.get("psg_output_max", 4096),
            psg_envelope_tables=s.get("psg_envelope_tables", {}),
        )


@dataclass
class SynthesisSettings:
    enabled: bool = False
    mode: str = "ym2612"
    clock_rate: int = 7_670_454       # YM2612 master clock
    amiga_clock: int = 3_546_895      # PAL Amiga clock for target_rate calc
    sustain: float = 1.5
    release: float = 0.5
    normalize_samples: bool = True    # True = peak-normalize to ±127; False = raw chip levels
    headroom_db: float = 6.0          # Base headroom below clipping applied to every carrier (dB)
    carrier_balance: bool = True      # Add extra TL per carrier count (normalises multi-carrier algos)

    @classmethod
    def from_yaml(cls, filepath: str) -> "SynthesisSettings":
        import yaml
        with open(filepath) as f:
            data = yaml.safe_load(f)
        s = data.get("synthesis", {})
        return cls(
            enabled=s.get("enabled", False),
            mode=s.get("mode", "ym2612"),
            clock_rate=s.get("clock_rate", 7_670_454),
            amiga_clock=s.get("amiga_clock", 3_546_895),
            sustain=s.get("sustain_duration", 1.5),
            release=s.get("release_padding", 0.5),
            normalize_samples=s.get("normalize_samples", True),
            headroom_db=s.get("headroom_db", 6.0),
            carrier_balance=s.get("carrier_balance", True),
        )


def derive_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps=60):
    """Derive MOD BPM from SMPS tempo parameters.

    The SMPS driver runs on VBlank. A tempo counter decrements each frame;
    when it hits 0, TempoWait fires — resetting the counter to `modifier`
    and adding +1 to all tracks' DurationTimeout (cancelling that frame's
    decrement). Effective tick rate = fps * (modifier - 1) / modifier.

    Note durations from assembly are multiplied by `divider`.

    MOD timing: rows_per_second = (BPM / 2.5) / speed

    Setting SMPS rows/sec equal to MOD rows/sec:
        BPM = fps * (modifier - 1) * speed * 2.5 / (modifier * divider * ticks_per_row)

    Args:
        tempo_divider: SMPS header divider (multiplies note durations)
        tempo_modifier: SMPS header modifier (TempoWait fires every N frames)
        ticks_per_row: SMPS ticks per MOD row
        speed: MOD speed (ticks per row in ProTracker)
        fps: Frame rate — 60 for NTSC, 50 for PAL

    Returns:
        Integer BPM clamped to 32–255
    """
    if tempo_modifier <= 1 or tempo_divider < 1:
        return 150  # fallback
    bpm = fps * (tempo_modifier - 1) * speed * 2.5 / (tempo_modifier * tempo_divider * ticks_per_row)
    return max(32, min(255, round(bpm)))


@dataclass
class ConversionConfig:
    name: str = "Untitled"
    input_file: str = ""
    output_file: str = "output.mod"
    target_bpm: int = 150
    target_speed: int = 6
    ticks_per_row: float = 6.0
    num_mod_channels: int = 10
    auto_bpm: bool = False        # Derive BPM from SMPS tempo header
    region: str = "ntsc"          # "ntsc" (60 Hz) or "pal" (50 Hz)
    channels: list = field(default_factory=list)       # list of ChannelConfig
    dac_samples: list = field(default_factory=list)    # list of DacSampleConfig
    sample_list: Optional[list] = None                 # [inst_num, filename, volume, finetune]
    samples_dir: str = "./samples/"
    max_patterns: int = 127
    voice_map: dict = field(default_factory=dict)         # {voice_index: list[InstrumentRange]}
    legacy_voice_map: dict = field(default_factory=dict)  # {voice_index: int} — deprecated simple form
    channel_instrument_map: dict = field(default_factory=dict)  # {source_channel: {voice_index: list[InstrumentRange]}}
    psg_map: dict = field(default_factory=dict)           # {form_byte_int: PsgInstrumentEntry}; type auto-inferred from bit 2
    psg_voice_map: dict = field(default_factory=dict)     # {"fTone_01": PsgInstrumentEntry, ...}

    @classmethod
    def default_sonic1(cls, song_name="Untitled"):
        """Create sensible defaults for a Sonic 1 song.

        10 channels: DAC→ch0, FM1-5→ch1-5, PSG1-3→ch6-8
        FM channels transposed -48 semitones, PSG -36 semitones
        """
        config = cls(
            name=song_name,
            num_mod_channels=10,
            target_bpm=150,
            target_speed=6,
            ticks_per_row=6.0,
        )

        # DAC channel
        config.channels.append(ChannelConfig(
            source="DAC", mod_channel=0, instrument=1
        ))

        # FM1-FM5
        # SMPS notes span C0-B7 (8 octaves), MOD spans C1-B3 (3 octaves).
        # Most Sonic 1 FM melodies sit in octaves 3-6, so -36 maps them to MOD range.
        for i in range(5):
            config.channels.append(ChannelConfig(
                source=f"FM{i+1}",
                mod_channel=i + 1,
                transpose=-36,
                instrument=i + 2,
            ))

        # PSG1-PSG3
        # PSG notes tend to be in higher octaves, -36 works here too.
        for i in range(3):
            config.channels.append(ChannelConfig(
                source=f"PSG{i+1}",
                mod_channel=i + 6,
                transpose=-36,
                instrument=i + 7,
            ))

        # Default DAC sample mappings
        config.dac_samples = [
            DacSampleConfig(name="dKick", mod_instrument=1, mod_note="C3"),
            DacSampleConfig(name="dSnare", mod_instrument=7, mod_note="C3"),
            DacSampleConfig(name="dTimpani", mod_instrument=8, mod_note="C3"),
            DacSampleConfig(name="dHiTimpani", mod_instrument=9, mod_note="C3"),
            DacSampleConfig(name="dMidTimpani", mod_instrument=10, mod_note="C3"),
            DacSampleConfig(name="dLowTimpani", mod_instrument=11, mod_note="C3"),
            DacSampleConfig(name="dVLowTimpani", mod_instrument=12, mod_note="C3"),
        ]

        return config

    @classmethod
    def from_yaml(cls, filepath):
        """Load configuration from a YAML file."""
        import yaml

        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)

        config = cls(
            name=data.get('name', 'Untitled'),
            input_file=data.get('input_file', ''),
            output_file=data.get('output_file', 'output.mod'),
            target_bpm=data.get('target_bpm', 150),
            target_speed=data.get('target_speed', 6),
            ticks_per_row=data.get('ticks_per_row', 6.0),
            num_mod_channels=data.get('num_mod_channels', 10),
            auto_bpm=data.get('auto_bpm', False),
            region=data.get('region', 'ntsc'),
            samples_dir=data.get('samples_dir', './samples/'),
            max_patterns=data.get('max_patterns', 127),
        )

        # Parse channel configs
        for ch_data in data.get('channels', []):
            config.channels.append(ChannelConfig(
                source=ch_data['source'],
                mod_channel=ch_data['mod_channel'],
                transpose=ch_data.get('transpose', 0),
                instrument=ch_data.get('instrument', 1),
                volume=ch_data.get('volume', 64),
                enabled=ch_data.get('enabled', True),
            ))

        # Parse DAC sample configs
        for dac_data in data.get('dac_samples', []):
            config.dac_samples.append(DacSampleConfig(
                name=dac_data['name'],
                mod_instrument=dac_data['mod_instrument'],
                mod_note=dac_data.get('mod_note', 'C3'),
            ))

        # ---------------------------------------------------------------------------
        # Parse voice_map
        #
        # New format:   voice_map: {0: [{low: G5, high: G6, mod_instrument: 4, root: F2s}]}
        # Legacy format: voice_map: {0: 4, 1: 5}  (simple int values — deprecated)
        # Old key name:  voice_instrument_map (deprecated — emit warning, parse as new voice_map)
        # ---------------------------------------------------------------------------

        # Step 1 — accept deprecated key voice_instrument_map (old name for new-format data)
        raw_vim_deprecated = data.get('voice_instrument_map')
        if raw_vim_deprecated is not None:
            warnings.warn(
                f"YAML key 'voice_instrument_map' in '{filepath}' is deprecated; "
                "rename it to 'voice_map'.",
                DeprecationWarning,
                stacklevel=2,
            )
            for voice_key, range_list in raw_vim_deprecated.items():
                config.voice_map[int(str(voice_key), 0)] = [
                    _parse_instrument_range(e) for e in range_list
                ]

        # Step 2 — read voice_map key: detect format by inspecting first value
        raw_vm = data.get('voice_map', {})
        if raw_vm:
            first_val = next(iter(raw_vm.values()))
            if isinstance(first_val, int):
                # Legacy simple format: {0: 4, 1: 5}
                warnings.warn(
                    f"YAML 'voice_map' with integer values in '{filepath}' is deprecated. "
                    "Use the list-of-ranges format (or remove it if voice_map covers all notes).",
                    DeprecationWarning,
                    stacklevel=2,
                )
                for k, v in raw_vm.items():
                    config.legacy_voice_map[int(str(k), 0)] = v
            else:
                # New list-of-ranges format — don't overwrite entries already set from
                # voice_instrument_map (step 1), in case both keys are present.
                for voice_key, range_list in raw_vm.items():
                    vk = int(str(voice_key), 0)
                    if vk not in config.voice_map:
                        config.voice_map[vk] = [
                            _parse_instrument_range(e) for e in range_list
                        ]

        # Parse channel_instrument_map — per-channel overrides for voice_map
        # {source_channel_name: {voice_idx: [InstrumentRange, ...]}}
        raw_cim = data.get('channel_instrument_map', {})
        for ch_name, vim_data in raw_cim.items():
            config.channel_instrument_map[ch_name] = {}
            for voice_key, range_list in vim_data.items():
                config.channel_instrument_map[ch_name][int(str(voice_key), 0)] = [
                    _parse_instrument_range(e) for e in range_list
                ]

        # Parse psg_map: dict keyed by smpsPSGform byte (hex or int YAML keys).
        # type is auto-inferred from bit 2 of the key byte:
        #   bit 2 = 1 → white noise ($E4–$E7); bit 2 = 0 → periodic noise ($E0–$E3)
        raw_psg_map = data.get('psg_map', {})
        for k, psg_entry in raw_psg_map.items():
            form_byte = int(str(k), 0)
            inferred_type = "white_noise" if (form_byte & 0x04) else "periodic_noise"
            root_note = ModNote[psg_entry['root']]
            synth_root = None
            if 'synth_root' in psg_entry:
                synth_root = parse_smps_note(psg_entry['synth_root'])
            config.psg_map[form_byte] = PsgInstrumentEntry(
                mod_instrument=psg_entry['mod_instrument'],
                type=inferred_type,
                root=root_note,
                synth_root=synth_root,
                noise_rate=psg_entry.get('noise_rate', 0),
                envelope=psg_entry.get('envelope', None),
                base_volume=psg_entry.get('base_volume', 0),
            )

        # psg_form_map is deprecated — psg_map now serves this role
        if 'psg_form_map' in data:
            warnings.warn(
                f"YAML key 'psg_form_map' in '{filepath}' is deprecated; "
                "merge entries into 'psg_map' (dict keyed by form byte).",
                DeprecationWarning,
                stacklevel=2,
            )

        # Parse psg_voice_map: {"fTone_01": {mod_instrument, root, envelope, ...}}
        raw_pvm = data.get('psg_voice_map', {})
        for k, v in raw_pvm.items():
            root_note = ModNote[v['root']]
            synth_root = None
            if 'synth_root' in v:
                synth_root = parse_smps_note(v['synth_root'])
            low = parse_smps_note(v['low']) if 'low' in v else None
            config.psg_voice_map[str(k)] = PsgInstrumentEntry(
                mod_instrument=v['mod_instrument'],
                type=v.get('type', 'tone'),
                root=root_note,
                synth_root=synth_root,
                low=low,
                noise_rate=v.get('noise_rate', 0),
                envelope=v.get('envelope', None),
                base_volume=v.get('base_volume', 0),
            )

        # Parse sample list
        config.sample_list = data.get('sample_list', None)

        return config
