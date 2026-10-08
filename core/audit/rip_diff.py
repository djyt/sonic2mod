"""A song against its VGM / VGZ rip, lifted: where the two play differently, note by note.

    asm / ROM song ──────────────────────────────── played_song ──┐
                                                                  ├─ align_songs, compare_songs ─► RipDiff
    rip ── lift_song (the song's tempo: modifier, divider) ── played_song ──┘

Both sides are read as the game shipped (data bugs kept): the rip recorded that.  The song states
its tempo exactly, so the lift is given its modifier (the tempo it starts at; changes are still
found) and divider; only where that tempo fits no schedule is it inferred, and RipDiff says so.
The lift matches each note to the song's own FM table (Golden Axe's is not Sonic 1's).
The channels compared are those both sides play, less what ChannelChoice leaves out; each is of
the kind the song says (RipDiff.kinds: Golden Axe's drum track is FM3 by name, DAC by kind).

A tie that changes nothing compared (smpsNoAttack at the same note, by default; with --aspects all
the same level, voice ... too) is merged into the note before it on both sides: it is heard as one
note, and a rip shows the read only where the driver writes the frequency on reads alone (Sonic
1's does; Type 0 FM writes it every frame).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..config import ConversionConfig
from ..smps import (
    Aspect,
    ChannelType,
    PlayedNote,
    PlayedSong,
    SmpsDriver,
    SmpsSong,
    SongDiff,
    TempoSegment,
    align_songs,
    compare_songs,
    frame_of_tick,
    played_song,
    source_map,
    tempo_schedule,
)
from ..source import is_vgm_path, read_song
from ..vgm import LIFTED_ASPECTS, VGM_SAMPLE_RATE, FrameLog, LiftOptions, VgmLiftError, lift_song


@dataclass(frozen=True)
class SongSource:
    """Where the expected song is read: an asm, or a ROM and its sound."""

    path: Path
    rom_song: int | None = None
    driver: SmpsDriver | None = None        # a ROM's variant; None: detected

    @classmethod
    def from_config(cls, config: str | Path, root: str | Path = ".") -> SongSource:
        """A config's input_file (relative to `root`), rom_song and driver."""
        cfg = ConversionConfig.from_yaml(str(config))
        if is_vgm_path(cfg.input_file):
            raise ValueError(f"{config}: input_file is a rip; compare its asm or ROM")
        return cls(Path(root) / cfg.input_file, cfg.rom_song, cfg.driver)

    @property
    def label(self) -> str:
        """'Mus81 - GHZ.asm', 'Moonwalker (World) (Rev A).md $81'."""
        return self.path.name if self.rom_song is None else f"{self.path.name} ${self.rom_song:02X}"

    def read(self) -> SmpsSong:
        return read_song(self.path, rom_song=self.rom_song, driver=self.driver, fix_data_bugs=False)


@dataclass(frozen=True)
class ChannelChoice:
    """The channels compared: names or prefixes ("FM": FM1-FM6), `only` empty for every one."""

    only: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()

    def picks(self, name: str) -> bool:
        if self.only and not name.startswith(self.only):
            return False
        return not (self.skip and name.startswith(self.skip))


_EVERY_CHANNEL = ChannelChoice()


class TempoSource(StrEnum):
    """Where the lift's tempo came from."""

    SONG = "the song's"
    INFERRED = "inferred"
    STATED = "stated"                       # the caller's LiftOptions


@dataclass(frozen=True)
class LiftTempo:
    """The tempo the rip was lifted at, and where it came from."""

    modifier: int
    divider: int
    source: TempoSource
    refused: str = ""                       # why the song's tempo was not used: the lift's error


@dataclass
class RipDiff:
    diff: SongDiff | None
    offset: int = 0                         # ticks the rip starts into the song
    tempo: LiftTempo | None = None
    only_song: list[str] = field(default_factory=list)     # channels only the song plays: not compared
    only_rip: list[str] = field(default_factory=list)      # ... only the rip plays
    error: str = ""
    kinds: dict[str, ChannelType] = field(default_factory=dict)     # each compared channel's
    schedule: tuple[TempoSegment, ...] = ()                 # the song's: when each tick plays
    fps: float = 0.0                                        # the rip's frames a second

    @property
    def ok(self) -> bool:
        return self.diff is not None and self.diff.ok

    def same_in(self, kinds: frozenset[str]) -> bool:
        """No difference on the channels of `kinds` (FM), nor in the song's tempo or loop."""
        if self.diff is None:
            return False
        return not self.diff.song and all(c.ok for c in self.diff.channels if self.kinds.get(c.name) in kinds)

    def seconds(self, tick: int) -> float:
        """When a tick of the song plays, from its start."""
        return frame_of_tick(self.schedule, tick) / self.fps


def compare_with_rip(song: SmpsSong, frames: FrameLog, aspects: frozenset[Aspect] = LIFTED_ASPECTS,
                     channels: ChannelChoice = _EVERY_CHANNEL, lift: LiftOptions | None = None) -> RipDiff:
    """Where `frames` (a rip) plays other than `song`, in `aspects` (default: what the lift reads)
    and the chosen channels both play.  `lift`: the lift's options instead of the song's tempo
    (LiftOptions(): inferred, to judge the inference)."""
    try:
        lifted, tempo = _lift(song, frames, lift)
    except VgmLiftError as e:
        return RipDiff(None, error=f"not lifted: {e}")

    # The channels both play, as chosen; ties that change nothing compared merged
    want, got = _merge_ties(played_song(song), aspects), _merge_ties(played_song(lifted), aspects)
    playing_want, playing_got = _playing(want, channels), _playing(got, channels)
    shared = playing_want & playing_got
    want, got = _only(want, shared), _only(got, shared)

    offset = align_songs(want, got)
    kinds = {name: channel.header.channel_type for name, channel in source_map(song).items() if name in shared}
    return RipDiff(compare_songs(want, got, aspects, offset), offset, tempo,
                   sorted(playing_want - shared), sorted(playing_got - shared), kinds=kinds,
                   schedule=tempo_schedule(want.modifier, want.tempo_changes, want.tempo_phase),
                   fps=VGM_SAMPLE_RATE / frames.frame_samples)


def _lift(song: SmpsSong, frames: FrameLog, options: LiftOptions | None) -> tuple[SmpsSong, LiftTempo]:
    """The rip lifted at `options`, else at the song's tempo, inferred where that fits no schedule."""
    if options is not None:
        lifted = lift_song(frames, dataclasses.replace(options, tempo_phase=song.header.tempo_phase,
                                                       fm_frequencies=song.fm_frequencies))
        stated = options.tempo_modifier is not None or options.tempo_divider is not None
        return lifted, _tempo(lifted, TempoSource.STATED if stated else TempoSource.INFERRED)

    h = song.header
    refused = ""
    if h.tempo_modifier:
        try:
            lifted = lift_song(frames, LiftOptions(tempo_modifier=h.tempo_modifier, tempo_divider=h.tempo_divider or None,
                                                   tempo_phase=h.tempo_phase, fm_frequencies=song.fm_frequencies))
            return lifted, _tempo(lifted, TempoSource.SONG)
        except VgmLiftError as e:
            refused = str(e)
    lifted = lift_song(frames, LiftOptions(tempo_phase=h.tempo_phase, fm_frequencies=song.fm_frequencies))
    return lifted, _tempo(lifted, TempoSource.INFERRED, refused)


def _tempo(lifted: SmpsSong, source: TempoSource, refused: str = "") -> LiftTempo:
    return LiftTempo(lifted.header.tempo_modifier, lifted.header.tempo_divider, source, refused)


def _playing(song: PlayedSong, channels: ChannelChoice) -> set[str]:
    """The chosen channels that play anything."""
    return {name for name, notes in song.channels.items() if channels.picks(name) and any(not p.rest for p in notes)}


# What a tie is, not what it changes: when it starts and how long it lasts
_TIE_ASPECTS = frozenset({Aspect.ONSET, Aspect.LENGTH})


def _merge_ties(song: PlayedSong, aspects: frozenset[Aspect]) -> PlayedSong:
    """Each channel with every tie that changes none of `aspects` folded into the note it continues."""
    kept = aspects - _TIE_ASPECTS
    return dataclasses.replace(song, channels={name: _merged(notes, kept) for name, notes in song.channels.items()})


def _merged(notes: list[PlayedNote], kept: frozenset[Aspect]) -> list[PlayedNote]:
    out: list[PlayedNote] = []
    for note in notes:
        if out and _continues(out[-1], note, kept):
            out[-1] = dataclasses.replace(out[-1], duration=out[-1].duration + note.duration)
            continue
        out.append(note)
    return out


def _continues(last: PlayedNote, note: PlayedNote, kept: frozenset[Aspect]) -> bool:
    """`note` is a tie of `last` that changes none of `kept`."""
    return (not note.attack and not note.rest and not last.rest and note.tick == last.tick + last.duration
            and all(note.aspect(a) == last.aspect(a) for a in kept))


def _only(song: PlayedSong, names: set[str]) -> PlayedSong:
    return dataclasses.replace(song, channels={n: p for n, p in song.channels.items() if n in names})
