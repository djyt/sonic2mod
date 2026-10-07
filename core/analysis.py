"""SMPS song analysis data model.

Walks parsed SmpsSong data and produces structured analysis objects
used by analyze.py for Rich-formatted display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .config import ConversionConfig
from .smps import (
    CoordFlag,
    SmpsChannel,
    SmpsEvent,
    SmpsNote,
    SmpsSong,
    pan_is_hard,
    semitone_to_note_name,  # noqa: F401 — re-exported for analyze.py
    source_names,
)

# ---------------------------------------------------------------------------
# Effect classification
# ---------------------------------------------------------------------------

UNSUPPORTED_EFFECTS = {CoordFlag.NOP}

# Detune is rendered into the samples (core/plan/detune.py) and smpsModSet is the driver's own
# modulation as 4xy (verified on six songs): both are supported, not partial.
PARTIAL_EFFECTS = {
    CoordFlag.PAN:      'no MOD panning: a hard pan counts as -3 dB (fm_pan_law_db)',
}

# Known Sonic 1 DAC samples: suggested MOD note and the rate real hardware plays them at - the
# Z80 loop's cycle count (core/rom/dac.py, exact from the ROM's code) less the 68k's once-a-frame
# stopZ80 (~5 %; each song's own share is measured in its VGZ, which the configs' finetunes
# follow).  The VGZ rips' emulator runs the loop 2-3 % fast: their rates are not the yardstick.
# docs/yaml_config.md § DAC Sample Rates.
DAC_NATIVE_INFO: dict[str, tuple[str, int]] = {
    'dKick':        ('B1',  7_790),
    'dSnare':       ('F3',  22_590),
    'dTimpani':     ('A1',  6_960),
    'dHiTimpani':   ('D2',  9_150),
    'dMidTimpani':  ('C2',  8_280),
    'dLowTimpani':  ('Gs1', 6_780),
    'dVLowTimpani': ('Gs1', 6_610),
}

# Timpani variants share a single MOD instrument (same WAV, different trigger note).
# Maps variant name → base/canonical name used for instrument slot assignment.
DAC_SAMPLE_GROUPS: dict[str, str] = {
    'dHiTimpani':   'dTimpani',
    'dMidTimpani':  'dTimpani',
    'dLowTimpani':  'dTimpani',
    'dVLowTimpani': 'dTimpani',
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
    # Most common (TL offset, hard-panned) level of this voice's notes on this channel — the level
    # fm_volume_scaling: baked treats as the instrument's own.  TL offset = smpsHeaderFM volume +
    # smpsAlterVol so far; hard-panned = smpsPan panLeft / panRight.
    modal_volume: int = 0
    modal_hard_pan: bool = False
    level_counts: dict = field(default_factory=dict)   # (tl, hard_pan) -> notes

@dataclass
class PsgToneStats:
    min_semitone: int      # raw SMPS (note_value - 0x81); _NO_NOTES_MIN if no notes
    max_semitone: int      # _NO_NOTES_MAX if no notes
    note_count: int
    switch_count: int
    modal_volume: int = 0     # most common SN76489 attenuation (header volume + smpsPSGAlterVol) of its notes
    level_counts: dict = field(default_factory=dict)   # attenuation -> notes

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
    # All effects seen: flag → count
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
        ts = PsgToneStats(_NO_NOTES_MIN, _NO_NOTES_MAX, 0, 0)
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
    walk = _ChannelWalk(ch, ch_type)

    # Config coverage
    config_enabled: bool | None = None
    uncovered_notes: list[int] = []
    if config is not None:
        ch_cfg = cfg_channels.get(source_name)
        config_enabled = ch_cfg.enabled if ch_cfg else False
        uncovered_notes = _uncovered_notes(walk.semitones, source_name, ch_type, config)

    return ChannelAnalysis(
        name=source_name,
        channel_type=ch_type,
        note_count=walk.note_count,
        rest_count=walk.rest_count,
        effect_count=walk.effect_count,
        total_ticks=walk.total_ticks,
        has_loop=ch.has_jump,
        loop_target=ch.loop_label,
        min_semitone=walk.min_semitone,
        max_semitone=walk.max_semitone,
        voice_stats=walk.voice_stats,
        psg_tone_stats=walk.psg_tone_stats,
        dac_counts=walk.dac_counts,
        effect_counts=walk.effect_counts,
        transpose_events=walk.transpose_events,
        has_transpose_change=len(walk.transpose_events) > 0,
        initial_transpose=ch.header.pitch_offset,
        uncovered_notes=uncovered_notes,
        config_enabled=config_enabled,
    )


class _ChannelWalk:
    """One channel's events, tallied: counts, note ranges, per-voice / per-tone stats, transpositions."""

    def __init__(self, ch: SmpsChannel, ch_type: str):
        self._ch_type = ch_type
        self.note_count = 0
        self.rest_count = 0
        self.effect_count = 0
        self.total_ticks = 0
        self.dac_counts: dict[str, int] = {}
        self.effect_counts: dict[CoordFlag, int] = {}
        self.voice_stats: dict[int, VoiceRangeStats] = {}
        self.psg_tone_stats: dict[str, PsgToneStats] = {}
        self.transpose_events: list[TransposeEvent] = []
        self.min_semitone: int | None = None
        self.max_semitone: int | None = None
        self.semitones: set[int] = set()   # every FM / PSG note played

        # Running totals as the driver keeps them: header pitch_offset (smpsHeaderFM $F4 = -12 for
        # FM1/FM3/FM4/FM5) plus every smpsChangeTransposition; header volume (TL offset) plus every
        # smpsAlterVol.
        self._transpose = ch.header.pitch_offset
        self._volume = ch.header.volume
        self._hard_pan = False
        self._voice_idx: int | None = None
        self._psg_label: str | None = None

        # A PSG channel starts on its header voice
        if ch_type == "PSG" and ch.header.psg_voice_label:
            self._psg_label = ch.header.psg_voice_label
            _get_or_create_psg_tone(self.psg_tone_stats, self._psg_label)

        self._walk(ch.events)
        self._settle_modal_levels()

    def _walk(self, events: list[SmpsEvent]) -> None:
        for event in events:
            if event.note is not None:
                self._note(event.note, event.tick_position)
            elif event.effect is not None:
                self._effect(event.effect.flag, event.effect.params, event.tick_position)

    def _settle_modal_levels(self) -> None:
        """Most common level per voice / PSG tone; ties go to the louder one, as in the converter."""
        for vs in self.voice_stats.values():
            if vs.level_counts:
                vs.modal_volume, vs.modal_hard_pan = max(
                    vs.level_counts, key=lambda lv: (vs.level_counts[lv], -lv[0], not lv[1]))
        for ts in self.psg_tone_stats.values():
            if ts.level_counts:
                ts.modal_volume = max(ts.level_counts, key=lambda a: (ts.level_counts[a], -a))

    def _note(self, note: SmpsNote, tick: int) -> None:
        self.total_ticks = tick + note.duration
        if note.is_rest:
            self.rest_count += 1
            return

        self.note_count += 1
        if note.is_dac:
            name = note.dac_name or f"${note.note_value:02X}"
            self.dac_counts[name] = self.dac_counts.get(name, 0) + 1
            return

        sem = note.note_value - 0x81
        self.semitones.add(sem)
        if self.min_semitone is None or sem < self.min_semitone:
            self.min_semitone = sem
        if self.max_semitone is None or sem > self.max_semitone:
            self.max_semitone = sem

        if self._ch_type == "PSG" and self._psg_label is not None:
            self._psg_note(self._psg_label, sem)
        if self._ch_type == "FM" and self._voice_idx is not None:
            self._fm_note(self._voice_idx, sem)

    def _psg_note(self, label: str, sem: int) -> None:
        ts = _get_or_create_psg_tone(self.psg_tone_stats, label)
        att = max(0, min(15, self._volume))
        ts.level_counts[att] = ts.level_counts.get(att, 0) + 1
        ts.note_count += 1
        _widen_range(ts, sem)

    def _fm_note(self, voice_idx: int, sem: int) -> None:
        vs = _get_or_create_voice(self.voice_stats, voice_idx)
        lv = (max(0, min(127, self._volume)), self._hard_pan)
        vs.level_counts[lv] = vs.level_counts.get(lv, 0) + 1
        vs.note_count += 1
        _widen_range(vs, sem)

    def _effect(self, kind: CoordFlag, params: list, tick: int) -> None:
        self.effect_count += 1
        self.effect_counts[kind] = self.effect_counts.get(kind, 0) + 1

        if kind == CoordFlag.SET_VOICE:
            self._switch_voice(cast(int, params[0]))
        elif kind == CoordFlag.PSG_VOICE:
            self._switch_psg(str(params[0]))
        elif kind == CoordFlag.PSG_FORM:
            self._switch_psg(f"form ${params[0]:02X}")
        elif kind == CoordFlag.ALTER_VOL:
            self._volume += cast(int, params[0])
        elif kind == CoordFlag.PAN:
            self._hard_pan = pan_is_hard(params)
        elif kind == CoordFlag.CHANGE_TRANSPOSITION:
            delta = params[0]
            self._transpose += delta
            self.transpose_events.append(TransposeEvent(
                tick=tick,
                delta=delta,
                cumulative=self._transpose,
            ))

    def _switch_voice(self, voice_idx: int) -> None:
        if voice_idx == self._voice_idx:
            return
        self._voice_idx = voice_idx
        _get_or_create_voice(self.voice_stats, voice_idx).switch_count += 1

    def _switch_psg(self, label: str) -> None:
        """smpsPSGvoice (envelope label) or smpsPSGform ("form $E7")."""
        if label == self._psg_label:
            return
        self._psg_label = label
        _get_or_create_psg_tone(self.psg_tone_stats, label).switch_count += 1


def _widen_range(stats: VoiceRangeStats | PsgToneStats, sem: int) -> None:
    stats.min_semitone = min(stats.min_semitone, sem)
    stats.max_semitone = max(stats.max_semitone, sem)


def _uncovered_notes(semitones: set[int], source_name: str, ch_type: str,
                     config: ConversionConfig) -> list[int]:
    """An FM channel's semitones that neither the voice_map nor its channel's instrument map covers."""
    if ch_type != "FM" or not config.voice_map:
        return []

    cim = config.channel_instrument_map.get(source_name, {})
    return [
        sem for sem in sorted(semitones)
        if not _is_semitone_covered(sem, config.voice_map)
        and not _is_semitone_covered(sem, cim)
    ]


def suggest_transpose(min_semitone: int, max_semitone: int) -> int:
    """Suggest a transpose value to map the note range into MOD C1-B3 (0-35).

    Tries to centre the range within the MOD octaves.
    """
    mid = (min_semitone + max_semitone) // 2
    # Target midpoint is ~17 (A2 in MOD range)
    target_mid = 17
    return target_mid - mid
