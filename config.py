"""Per-song conversion configuration."""

from dataclasses import dataclass, field
from typing import Optional

from tables import ModNote, parse_smps_note


@dataclass
class InstrumentRange:
    low: int            # inclusive lower bound — SMPS semitone from C0 (e.g. nA2 = 33)
    high: int           # inclusive upper bound — SMPS semitone from C0
    instrument: int     # MOD instrument number (1-31)
    root: Optional[ModNote] = None  # MOD note where `low` plays;
                                    # out_note = root + (source_semitone - low)
                                    # if None: fall back to channel transpose for note


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
class SynthesisSettings:
    enabled: bool = False
    mode: str = "ym2612"
    clock_rate: int = 7_670_454       # YM2612 master clock
    amiga_clock: int = 3_546_895      # PAL Amiga clock for target_rate calc
    sustain: float = 1.5
    release: float = 0.5

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
    dac_samples: list = field(default_factory=list)     # list of DacSampleConfig
    sample_list: Optional[list] = None                  # [inst_num, filename, volume, finetune]
    samples_dir: str = "./samples/"
    max_patterns: int = 127
    voice_map: dict = field(default_factory=dict)  # {voice_index: mod_instrument}
    voice_instrument_map: dict = field(default_factory=dict)  # {voice_index: list[InstrumentRange]}

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

        # Parse voice_map — keys may be int or 0x-prefixed hex strings in YAML
        raw_vm = data.get('voice_map', {})
        config.voice_map = {int(str(k), 0): v for k, v in raw_vm.items()}

        # Parse voice_instrument_map
        # low/high are SMPS note names (e.g. 'G5', 'Cs6') → semitone from C0
        # root is a ModNote name (e.g. 'F2s', 'A1') → output MOD note anchor
        raw_vim = data.get('voice_instrument_map', {})
        for voice_key, range_list in raw_vim.items():
            parsed_ranges = []
            for entry in range_list:
                low = parse_smps_note(entry['low'])
                high = parse_smps_note(entry['high'])
                inst = entry['instrument']
                root = ModNote[entry['root']] if 'root' in entry else None
                parsed_ranges.append(InstrumentRange(low=low, high=high, instrument=inst, root=root))
            config.voice_instrument_map[int(str(voice_key), 0)] = parsed_ranges

        # Parse sample list
        config.sample_list = data.get('sample_list', None)

        return config
