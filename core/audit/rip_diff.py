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

A tie that changes nothing compared (smpsNoAttack at the same note, by default; with every aspect
compared the same level, voice ... too) is merged into the note before it on both sides: it is heard as one
note, and a rip shows the read only where the driver writes the frequency on reads alone (Sonic
1's does; Type 0 FM writes it every frame).

The rip's own faults (rips.yaml, RipFaults) are not the song's: its glitches are undone before the
lift, a channel another sound holds throughout is not compared, and the differences where one
holds a channel for a while are set aside, counted (RipDiff.foreign).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..chips import freq_word_hz
from ..config import ConversionConfig
from ..smps import (
    FM_CHANNEL_NAMES,
    Aspect,
    ChannelDiff,
    ChannelType,
    PlayedNote,
    PlayedSong,
    SmpsSong,
    SongDiff,
    TempoSegment,
    align_songs,
    compare_songs,
    frame_of_tick,
    played_song,
    source_map,
    tempo_schedule,
    tick_at_frame,
)
from ..source import SmpsDriver, is_vgm_path, read_dac, read_song
from ..vgm import LIFTED_ASPECTS, FrameLog, LiftOptions, VgmLiftError, lift_song, load_frames
from .rips import RipFaults, dac_written, frame_cut


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
        return f"{self.path.name} {self.sound}".rstrip()

    @property
    def sound(self) -> str:
        """A ROM song's sound, '$81'; '' for an asm."""
        return "" if self.rom_song is None else f"${self.rom_song:02X}"

    def read(self) -> SmpsSong:
        return read_song(self.path, rom_song=self.rom_song, driver=self.driver, fix_data_bugs=False)

    def rip_frames(self, rip: Path, song: SmpsSong, cache_dir: str | Path | None = None) -> FrameLog:
        """`rip`'s frame log as `song` (this source's) is played: cut as its driver's update runs,
        its DAC starts found by the ROM's samples' bytes (an asm's: by seeks and pauses)."""
        return load_frames(rip, cache_dir, frame_cut(song), dac_written(read_dac(self.path, self.driver)))


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
_NO_FAULTS = RipFaults()


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
    """A song against its rip: the differences (None: not compared, `error` says why) and how they
    were found."""

    diff: SongDiff | None
    offset: int = 0                         # ticks the rip starts into the song
    tempo: LiftTempo | None = None
    only_song: list[str] = field(default_factory=list)     # channels only the song plays: not compared
    only_rip: list[str] = field(default_factory=list)      # ... only the rip plays
    error: str = ""
    kinds: dict[str, ChannelType] = field(default_factory=dict)     # each compared channel's
    schedule: tuple[TempoSegment, ...] = ()                 # the song's: when each tick plays
    fps: float = 0.0                                        # the rip's frames a second
    foreign: dict[str, int] = field(default_factory=dict)   # known rip faults: differences set aside, per channel
    foreign_channels: list[str] = field(default_factory=list)   # ... channels another sound holds throughout

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
                     channels: ChannelChoice = _EVERY_CHANNEL, lift: LiftOptions | None = None,
                     faults: RipFaults = _NO_FAULTS) -> RipDiff:
    """Where `frames` (a rip) plays other than `song`, in `aspects` (default: what the lift reads)
    and the chosen channels both play.  `lift`: the lift's options instead of the song's tempo
    (LiftOptions(): inferred, to judge the inference).  The lift reads by the song's rules.
    `faults`: the rip's own (rips.yaml), undone or set aside."""
    frames = faults.realign(frames)
    try:
        lifted, tempo = _lift(song, frames, lift)
    except VgmLiftError as e:
        return RipDiff(None, error=f"not lifted: {e}")

    # The channels both play, as chosen, less those another sound holds throughout; ties that
    # change nothing compared merged
    played = played_song(lifted)
    fm = _fm_channels(song)
    want, got = _merge_ties(_as_lifted(played_song(song), song), aspects, fm), _merge_ties(played, aspects, fm)
    playing_want, playing_got = _playing(want, channels), _playing(got, channels)
    foreign = (playing_want | playing_got) & faults.foreign_throughout()
    shared = (playing_want & playing_got) - foreign
    want, got = _only(want, shared), _only(got, shared)

    offset = align_songs(want, got)
    kinds = {name: channel.header.channel_type for name, channel in source_map(song).items() if name in shared}
    diff = compare_songs(want, got, aspects, offset, _recorded_end(played, frames, offset))
    aside = _set_aside(diff, faults, _rip_frame(played, frames, offset)) if faults.foreign else {}
    return RipDiff(diff, offset, tempo, sorted(playing_want - shared - foreign), sorted(playing_got - shared - foreign),
                   kinds=kinds, schedule=tempo_schedule(want.modifier, want.tempo_changes, want.tempo_phase),
                   fps=frames.fps, foreign=aside, foreign_channels=sorted(foreign))


def _rip_frame(lifted: PlayedSong, frames: FrameLog, offset: int) -> Callable[[int], int]:
    """A song tick's frame in the (realigned) rip."""
    schedule, start = _rip_clock(lifted, frames)
    return lambda tick: start + frame_of_tick(schedule, max(tick - offset, 0))


def _rip_clock(lifted: PlayedSong, frames: FrameLog) -> tuple[tuple[TempoSegment, ...], int]:
    """The lift's tick schedule, and the rip frame its tick 0 is on: its first FM note plays on the
    rip's first FM key-on (a song with none: its first note on the first frame that writes anything;
    a ripper's opening state and its DAC writes, Time Trax's first 40 frames, key nothing)."""
    schedule = tempo_schedule(lifted.modifier, lifted.tempo_changes, lifted.tempo_phase)
    fm = [n.tick for name, notes in lifted.channels.items() if name in FM_CHANNEL_NAMES for n in notes if not n.rest]
    keyed = next((f.index for f in frames.frames if any(any(ch.keys) for ch in f.fm)), None)
    if fm and keyed is not None:
        return schedule, keyed - frame_of_tick(schedule, min(fm))
    first = min((n.tick for notes in lifted.channels.values() for n in notes if not n.rest), default=0)
    return schedule, next((f.index for f in frames.frames if f.active), 0) - frame_of_tick(schedule, first)


def _recorded_end(lifted: PlayedSong, frames: FrameLog, offset: int) -> int:
    """The song tick of the rip's last frame, which it may have caught half written (a frequency
    and no key): nothing from it on is compared.  Past it the lifted song is the log's loop played
    again, not the game."""
    schedule, start = _rip_clock(lifted, frames)
    return tick_at_frame(schedule, len(frames.frames) - 1 - start) + offset


def _set_aside(diff: SongDiff, faults: RipFaults, rip_frame: Callable[[int], int]) -> dict[str, int]:
    """The differences on frames another sound holds taken out of `diff`: how many, per channel."""
    aside: dict[str, int] = {}
    for i, channel in enumerate(diff.channels):
        def mine(tick: int, name: str = channel.name) -> bool:
            return not faults.foreign_at(name, rip_frame(tick))

        clean = ChannelDiff(channel.name, channel.notes, [t for t in channel.missing if mine(t)],
                            [t for t in channel.extra if mine(t)], [d for d in channel.changed if mine(d.tick)])
        dropped = _differences(channel) - _differences(clean)
        if dropped:
            aside[channel.name] = dropped
            diff.channels[i] = clean
    return aside


def _differences(channel: ChannelDiff) -> int:
    return len(channel.missing) + len(channel.extra) + len(channel.changed)


def _lift(song: SmpsSong, frames: FrameLog, options: LiftOptions | None) -> tuple[SmpsSong, LiftTempo]:
    """The rip lifted at `options`, else at the song's tempo, inferred where that fits no schedule."""
    if options is not None:
        lifted = lift_song(frames, song.rules, options)
        stated = options.tempo_modifier is not None or options.tempo_divider is not None
        return lifted, _tempo(lifted, TempoSource.STATED if stated else TempoSource.INFERRED)

    h = song.header
    refused = "none stated"
    if h.tempo_modifier:
        try:
            lifted = lift_song(frames, song.rules,
                               LiftOptions(tempo_modifier=h.tempo_modifier, tempo_divider=h.tempo_divider or None))
            return lifted, _tempo(lifted, TempoSource.SONG)
        except VgmLiftError as e:
            refused = str(e)
    lifted = lift_song(frames, song.rules)
    return lifted, _tempo(lifted, TempoSource.INFERRED, refused)


def _tempo(lifted: SmpsSong, source: TempoSource, refused: str = "") -> LiftTempo:
    return LiftTempo(lifted.header.tempo_modifier, lifted.header.tempo_divider, source, refused)


def _playing(song: PlayedSong, channels: ChannelChoice) -> set[str]:
    """The chosen channels that play anything."""
    return {name for name, notes in song.channels.items() if channels.picks(name) and any(not p.rest for p in notes)}


# What a tie is, not what it changes: when it starts and how long it lasts; and on FM its note, which
# the lift reads only where a key write marks the tie (a legato slide writes a frequency alone; the
# PSG lift reads each period write)
_TIE_ASPECTS = frozenset({Aspect.ONSET, Aspect.LENGTH})
_FM_TIE_ASPECTS = _TIE_ASPECTS | {Aspect.NOTE}


def _as_lifted(played: PlayedSong, song: SmpsSong) -> PlayedSong:
    """The song's FM notes as the lift reads a note: the table entry nearest the pitch that sounds,
    its detune and channel 3's offset in (Streets of Rage 2's $F2 transposes), not the one the
    song names."""
    fm = _fm_channels(song)
    table = song.rules.fm_frequencies[1:]                    # entry 0 is no note
    if not table:
        return played
    hz = [math.log2(freq_word_hz(word)) for word in table]

    def nearest(note: PlayedNote) -> PlayedNote:
        if note.rest or note.pitch is None or note.pitch == note.note or freq_word_hz(note.pitch) <= 0:
            return note
        at = math.log2(freq_word_hz(note.pitch))
        return dataclasses.replace(note, note=table[min(range(len(hz)), key=lambda i: abs(hz[i] - at))])

    return dataclasses.replace(played, channels={name: [nearest(n) for n in notes] if name in fm else notes
                                                 for name, notes in played.channels.items()})


def _merge_ties(song: PlayedSong, aspects: frozenset[Aspect], fm: set[str]) -> PlayedSong:
    """Each channel with every tie that changes none of `aspects` folded into the note it continues
    (an FM tie's note aside: `fm`, those channels)."""
    return dataclasses.replace(song, channels={
        name: _merged(notes, aspects - (_FM_TIE_ASPECTS if name in fm else _TIE_ASPECTS))
        for name, notes in song.channels.items()})


def _fm_channels(song: SmpsSong) -> set[str]:
    return {name for name, ch in source_map(song).items() if ch.header.channel_type == ChannelType.FM}


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
