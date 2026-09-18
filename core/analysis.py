"""SMPS song analysis data model.

Walks parsed SmpsSong data and produces structured analysis objects
used by analyze.py for Rich-formatted display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .config import ConversionConfig
from .driver_state import pan_is_hard, source_names
from .smps_parser import SmpsChannel, SmpsSong
from .tables import semitone_to_note_name  # noqa: F401 — re-exported for analyze.py

# ---------------------------------------------------------------------------
# Effect classification
# ---------------------------------------------------------------------------

UNSUPPORTED_EFFECTS = {'smpsPan', 'smpsNop'}

PARTIAL_EFFECTS = {
    'smpsAlterNote':   'FNUM offset (~10 cents) — not applied to pitch',
    'smpsModSet':      'approximate (sine vs triangle wave)',
}

# Known Sonic 1 DAC sample native playback rates and suggested MOD notes.
# Notes and rates from docs/yaml_config.md § DAC Sample Rates.
DAC_NATIVE_INFO: dict[str, tuple[str, int]] = {
    'dKick':        ('C2',  8_250),
    'dSnare':       ('Fs3', 24_000),
    'dTimpani':     ('As1', 7_375),
    'dHiTimpani':   ('Ds2', 9_588),
    'dMidTimpani':  ('Cs2', 8_850),
    'dLowTimpani':  ('A1',  7_154),
    'dVLowTimpani': ('A1',  7_006),
}

# Timpani variants share a single MOD instrument (same WAV, different trigger note).
# Maps variant name → base/canonical name used for instrument slot assignment.
DAC_SAMPLE_GROUPS: dict[str, str] = {
    'dHiTimpani':   'dTimpani',
    'dMidTimpani':  'dTimpani',
    'dLowTimpani':  'dTimpani',
    'dVLowTimpani': 'dTimpani',
}

# YM2612 carrier operator register offsets by algorithm (0–7).
# Used to describe which operators carry audio output.
_CARRIER_LABELS_BY_ALG = {
    0: ['OP4'],
    1: ['OP4'],
    2: ['OP4'],
    3: ['OP4'],
    4: ['OP2', 'OP4'],
    5: ['OP2', 'OP3', 'OP4'],
    6: ['OP2', 'OP3', 'OP4'],
    7: ['OP1', 'OP2', 'OP3', 'OP4'],
}

# Sentinel values for "no notes seen yet" in min/max semitone tracking.
_NO_NOTES_MIN = 999
_NO_NOTES_MAX = -1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VoiceRangeStats:
    voice_idx: int
    min_semitone: int      # raw SMPS (note_value - 0x81); _NO_NOTES_MIN if no notes
    max_semitone: int      # _NO_NOTES_MAX if no notes
    note_count: int
    switch_count: int      # how many smpsSetvoice events switched to this voice
    modal_transpose: int = 0  # cumulative_transpose at time of first note for this voice
    # Most common (TL offset, hard-panned) level of this voice's notes on this channel — the level
    # fm_volume_scaling: baked treats as the instrument's own.  TL offset = smpsHeaderFM volume +
    # smpsAlterVol so far; hard-panned = smpsPan panLeft / panRight.
    modal_volume: int = 0
    modal_hard_pan: bool = False
    level_counts: dict = field(default_factory=dict)   # (tl, hard_pan) -> notes

    def has_notes(self) -> bool:
        return self.note_count > 0


@dataclass
class PsgToneStats:
    tone_label: str        # e.g. "fTone_06", "form $E7"
    min_semitone: int      # raw SMPS (note_value - 0x81); _NO_NOTES_MIN if no notes
    max_semitone: int      # _NO_NOTES_MAX if no notes
    note_count: int
    switch_count: int
    modal_volume: int = 0     # most common SN76489 attenuation (header volume + smpsPSGAlterVol) of its notes
    level_counts: dict = field(default_factory=dict)   # attenuation -> notes

    def has_notes(self) -> bool:
        return self.note_count > 0


@dataclass
class TransposeEvent:
    tick: int
    delta: int             # this event's semitone delta
    cumulative: int        # total after this event


@dataclass
class ChannelAnalysis:
    name: str              # "DAC", "FM1", "PSG2", etc.
    channel_type: str      # "DAC", "FM", "PSG"
    note_count: int
    rest_count: int
    effect_count: int
    total_ticks: int
    has_loop: bool
    loop_target: str
    # FM/PSG note ranges (None for DAC)
    min_semitone: int | None
    max_semitone: int | None
    # Per-voice stats (FM channels only)
    voice_stats: dict = field(default_factory=dict)   # voice_idx -> VoiceRangeStats
    # Per-tone stats (PSG channels only)
    psg_tone_stats: dict = field(default_factory=dict)  # tone_label -> PsgToneStats
    # DAC sample occurrence counts
    dac_counts: dict[str, int] = field(default_factory=dict)
    # All effects seen: effect_type → count
    effect_counts: dict = field(default_factory=dict)
    # smpsChangeTransposition history
    transpose_events: list = field(default_factory=list)
    has_transpose_change: bool = False
    # Initial cumulative_transpose (= smpsHeaderFM pitch_offset, e.g. -12 for $F4).
    # Raw SMPS semitones + initial_transpose ≈ effective semitones at song start.
    initial_transpose: int = 0
    # Config coverage gaps (populated only if config provided)
    uncovered_notes: list = field(default_factory=list)   # semitones not in voice_map ranges
    # Config enabled status (populated if config provided)
    config_enabled: bool | None = None


@dataclass
class SongAnalysis:
    file_path: str
    song: SmpsSong
    channels: list[ChannelAnalysis]
    config: ConversionConfig | None
    derived_bpm_ntsc: float
    derived_bpm_pal: float


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _get_or_create_voice(voice_stats: dict, voice_idx: int) -> VoiceRangeStats:
    """Get or insert a VoiceRangeStats entry initialised with sentinel min/max."""
    vs = voice_stats.get(voice_idx)
    if vs is None:
        vs = VoiceRangeStats(
            voice_idx=voice_idx,
            min_semitone=_NO_NOTES_MIN,
            max_semitone=_NO_NOTES_MAX,
            note_count=0,
            switch_count=0,
        )
        voice_stats[voice_idx] = vs
    return vs


def _get_or_create_psg_tone(psg_tone_stats: dict, label: str) -> PsgToneStats:
    """Get or insert a PsgToneStats entry initialised with sentinel min/max."""
    ts = psg_tone_stats.get(label)
    if ts is None:
        ts = PsgToneStats(label, _NO_NOTES_MIN, _NO_NOTES_MAX, 0, 0)
        psg_tone_stats[label] = ts
    return ts


def _is_semitone_covered(sem: int, voice_map_dict: dict) -> bool:
    """Return True if sem falls within any InstrumentRange in the given voice_map dict."""
    for ranges in voice_map_dict.values():
        for entry in ranges:
            if entry.low <= sem <= entry.high:
                return True
    return False


# ---------------------------------------------------------------------------
# Analysis function
# ---------------------------------------------------------------------------

def _source_name(ch_type: str, idx: int) -> str:
    if ch_type == "DAC":
        return "DAC"
    elif ch_type == "FM":
        return f"FM{idx}"
    else:
        return f"PSG{idx}"


def analyze_song(song: SmpsSong, file_path: str,
                 config: ConversionConfig | None = None) -> SongAnalysis:
    """Walk each channel's events and produce a SongAnalysis.

    Args:
        song:       Parsed SmpsSong from SmpsParser.
        file_path:  Path to the source .asm file (for display).
        config:     Optional ConversionConfig — if provided, coverage gaps are reported.

    Returns:
        SongAnalysis with per-channel ChannelAnalysis objects.
    """
    from .config import derive_bpm

    # Derive BPM for both regions
    # Use default ticks_per_row=6, speed=6 (common Sonic 1 defaults)
    tpr = config.ticks_per_row if config else 6.0
    spd = config.target_speed if config else 6
    bpm_ntsc = derive_bpm(
        song.header.tempo_divider, song.header.tempo_modifier, tpr, spd, fps=60
    )
    bpm_pal = derive_bpm(
        song.header.tempo_divider, song.header.tempo_modifier, tpr, spd, fps=50
    )

    # Build config channel lookup if config provided
    cfg_channels = {}
    if config:
        for ch_cfg in config.channels:
            cfg_channels[ch_cfg.source] = ch_cfg

    channel_analyses = [
        _analyze_channel(ch, source_name, ch.header.channel_type, config, cfg_channels)
        for ch, source_name in zip(song.channels, source_names(song), strict=True)
    ]

    return SongAnalysis(
        file_path=file_path,
        song=song,
        channels=channel_analyses,
        config=config,
        derived_bpm_ntsc=bpm_ntsc,
        derived_bpm_pal=bpm_pal,
    )


def _analyze_channel(ch: SmpsChannel, source_name: str, ch_type: str,
                     config: ConversionConfig | None,
                     cfg_channels: dict) -> ChannelAnalysis:
    """Analyze a single SmpsChannel."""
    note_count = 0
    rest_count = 0
    effect_count = 0
    dac_counts: dict[str, int] = {}
    effect_counts: dict[str, int] = {}
    voice_stats: dict[int, VoiceRangeStats] = {}
    transpose_events: list[TransposeEvent] = []
    min_semitone: int | None = None
    max_semitone: int | None = None

    current_voice_idx: int | None = None
    psg_tone_stats: dict[str, PsgToneStats] = {}
    current_psg_label: str | None = None
    if ch_type == "PSG":
        initial_label = ch.header.psg_voice_label
        if initial_label:
            current_psg_label = initial_label
            psg_tone_stats[initial_label] = PsgToneStats(
                tone_label=initial_label,
                min_semitone=_NO_NOTES_MIN, max_semitone=_NO_NOTES_MAX,
                note_count=0, switch_count=0,
            )
    # Initialise to header pitch_offset so cumulative reflects the true
    # running total (smpsHeaderFM $F4 = -12 for FM1/FM3/FM4/FM5).
    cumulative_transpose = ch.header.pitch_offset
    # Same for the TL offset: smpsHeaderFM volume, then every smpsAlterVol adds to it.
    cumulative_volume = ch.header.volume
    hard_pan = False
    total_ticks = 0

    for event in ch.events:
        if event.is_note:
            note = event.note
            total_ticks = event.tick_position + note.duration

            if note.is_rest:
                rest_count += 1
            elif note.is_dac:
                note_count += 1
                name = note.dac_name or f"${note.note_value:02X}"
                dac_counts[name] = dac_counts.get(name, 0) + 1
            else:
                note_count += 1
                sem = note.note_value - 0x81

                # Update global range
                if min_semitone is None or sem < min_semitone:
                    min_semitone = sem
                if max_semitone is None or sem > max_semitone:
                    max_semitone = sem

                # Update per-tone stats (PSG channels only)
                if ch_type == "PSG" and current_psg_label is not None:
                    ts = _get_or_create_psg_tone(psg_tone_stats, current_psg_label)
                    _att = max(0, min(15, cumulative_volume))
                    ts.level_counts[_att] = ts.level_counts.get(_att, 0) + 1
                    ts.note_count += 1
                    if sem < ts.min_semitone:
                        ts.min_semitone = sem
                    if sem > ts.max_semitone:
                        ts.max_semitone = sem

                # Update per-voice stats (FM channels only)
                if ch_type == "FM" and current_voice_idx is not None:
                    vs = _get_or_create_voice(voice_stats, current_voice_idx)
                    if vs.note_count == 0:
                        # First note for this voice — record transpose and initial range
                        vs.modal_transpose = cumulative_transpose
                    _lv = (max(0, min(127, cumulative_volume)), hard_pan)
                    vs.level_counts[_lv] = vs.level_counts.get(_lv, 0) + 1
                    vs.note_count += 1
                    if sem < vs.min_semitone:
                        vs.min_semitone = sem
                    if sem > vs.max_semitone:
                        vs.max_semitone = sem

        elif event.is_effect:
            eff = event.effect
            effect_count += 1
            effect_counts[eff.effect_type] = effect_counts.get(eff.effect_type, 0) + 1

            if eff.effect_type == 'smpsSetvoice':
                new_voice = cast(int, eff.params[0])
                if new_voice != current_voice_idx:
                    current_voice_idx = new_voice
                    vs = _get_or_create_voice(voice_stats, current_voice_idx)
                    vs.switch_count += 1

            elif eff.effect_type == 'smpsPSGvoice':
                label = str(eff.params[0])
                if label != current_psg_label:
                    current_psg_label = label
                    ts = _get_or_create_psg_tone(psg_tone_stats, label)
                    ts.switch_count += 1

            elif eff.effect_type == 'smpsPSGform':
                label = f"form ${eff.params[0]:02X}"
                if label != current_psg_label:
                    current_psg_label = label
                    ts = _get_or_create_psg_tone(psg_tone_stats, label)
                    ts.switch_count += 1

            elif eff.effect_type == 'smpsAlterVol':
                cumulative_volume += cast(int, eff.params[0])

            elif eff.effect_type == 'smpsPan':
                hard_pan = pan_is_hard(eff.params)

            elif eff.effect_type == 'smpsChangeTransposition':
                delta = eff.params[0]
                cumulative_transpose += delta
                transpose_events.append(TransposeEvent(
                    tick=event.tick_position,
                    delta=delta,
                    cumulative=cumulative_transpose,
                ))

    # Most common level per voice / PSG tone; ties go to the louder one, as in the converter.
    for vs in voice_stats.values():
        if vs.level_counts:
            vs.modal_volume, vs.modal_hard_pan = max(
                vs.level_counts, key=lambda lv: (vs.level_counts[lv], -lv[0], not lv[1]))
    for ts in psg_tone_stats.values():
        if ts.level_counts:
            ts.modal_volume = max(ts.level_counts, key=lambda a: (ts.level_counts[a], -a))

    has_transpose_change = len(transpose_events) > 0

    # Config coverage
    config_enabled: bool | None = None
    uncovered_notes: list[int] = []

    if config is not None:
        ch_cfg = cfg_channels.get(source_name)
        config_enabled = ch_cfg.enabled if ch_cfg else False

        # For FM channels with a voice_map, check which semitones are uncovered
        if ch_type == "FM" and config.voice_map:
            seen_semitones: set[int] = set()
            for event in ch.events:
                if event.is_note and not event.note.is_rest and not event.note.is_dac:
                    seen_semitones.add(event.note.note_value - 0x81)

            cim = config.channel_instrument_map.get(source_name, {})
            uncovered_notes.extend(
                sem for sem in sorted(seen_semitones)
                if not _is_semitone_covered(sem, config.voice_map)
                and not _is_semitone_covered(sem, cim)
            )

    return ChannelAnalysis(
        name=source_name,
        channel_type=ch_type,
        note_count=note_count,
        rest_count=rest_count,
        effect_count=effect_count,
        total_ticks=total_ticks,
        has_loop=ch.has_jump,
        loop_target=ch.jump_target_label,
        min_semitone=min_semitone,
        max_semitone=max_semitone,
        voice_stats=voice_stats,
        psg_tone_stats=psg_tone_stats,
        dac_counts=dac_counts,
        effect_counts=effect_counts,
        transpose_events=transpose_events,
        has_transpose_change=has_transpose_change,
        initial_transpose=ch.header.pitch_offset,
        uncovered_notes=uncovered_notes,
        config_enabled=config_enabled,
    )


def suggest_transpose(min_semitone: int, max_semitone: int) -> int:
    """Suggest a transpose value to map the note range into MOD C1-B3 (0-35).

    Tries to centre the range within the MOD octaves.
    """
    mid = (min_semitone + max_semitone) // 2
    # Target midpoint is ~17 (A2 in MOD range)
    target_mid = 17
    return target_mid - mid
