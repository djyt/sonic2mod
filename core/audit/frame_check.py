"""A song against its rip's frame log, note by note and with no lift: the chip's registers on
each note's frame (core.vgm.frames), where the song says the note starts.

The lift (rip_diff.py) reads notes back from the log, so it cannot see a detune, a voice or a slide
that writes no key; this reads what the chip was given, for any driver:

    pitch   FM: the frequency word (block, fnum), detune in; PSG: the tone divider (noise: tone 3's, 0 read as 1)
    level   FM: the carriers' TL; PSG: the attenuation with the envelope's first step, which the
            driver adds on the key-on frame (clamped to silence; noise: the noise channel's)
    voice   FM: the feedback / algorithm and every operator register but the carriers' TL, the bits
            the chip reads (a driver may write B0 with the voice's byte whole)

Attacking notes and tied ones are counted apart: vibrato runs on through a tie, so a tied note's
pitch is the song's and the vibrato so far, its level the envelope's step so far.  A silent PSG
note's pitch is not judged (the driver need not write it, and nothing hears it).

Where the rip's song began is the rip's: the offset is the commonest difference, over the FM
channels (whichever are checked), between a channel's first attack in the song and its first key-on
in the rip; and TempoWait's holds may fall a frame earlier than the song's phase says (the
counter's state when the game started the song: Sonic 1's Special Stage and Chaos Emerald rips),
so the phase that matches more notes is taken (FrameCheck.holds_early).  A drum track is not read (a DAC sample, or Type 0 FM's FM drums on FM3).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from ..chips import FEEDBACK_ALGORITHM_MASK, PSG_ATT_SILENT, freq_word, operator_bits
from ..smps import (
    FM_CHANNEL_NAMES,
    PSG_CHANNEL_NAMES,
    ChannelType,
    PlayedNote,
    PlayedSong,
    SmpsSong,
    frame_of_tick,
    played_song,
    source_map,
    tempo_schedule,
)
from ..vgm import NOISE_CHANNEL, FrameLog
from .rip_diff import ChannelChoice

_EVERY_CHANNEL = ChannelChoice()
_NOISE_TONE = PSG_CHANNEL_NAMES.index("PSG3")      # the tone channel that clocks the noise


class FrameAspect(StrEnum):
    PITCH = "pitch"
    LEVEL = "level"
    VOICE = "voice"


@dataclass(frozen=True)
class FrameMiss:
    """A note whose aspect the rip's frame holds otherwise."""

    channel: str
    tick: int
    tied: bool
    aspect: FrameAspect
    want: object                        # the song's
    got: object                         # the rip's


@dataclass
class FrameCheck:
    offset: int                         # frames the rip starts into the song
    holds_early: bool = False           # the rip's TempoWait holds a frame earlier than the song's phase
    checked: Counter[tuple[str, FrameAspect, bool]] = field(default_factory=Counter)   # (channel, aspect, tied)
    misses: list[FrameMiss] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every attacking note as the rip has it."""
        return not any(not m.tied for m in self.misses)

    def missed(self) -> Counter[tuple[str, FrameAspect, bool]]:
        return Counter((m.channel, m.aspect, m.tied) for m in self.misses)


def check_frames(song: SmpsSong, frames: FrameLog, channels: ChannelChoice = _EVERY_CHANNEL) -> FrameCheck:
    """Each FM and PSG note `song` plays against `frames` on its frame, at the hold phase that
    matches the rip better (the song's first)."""
    played = played_song(song)
    melodic = {name for name, ch in source_map(song).items() if ch.header.channel_type != ChannelType.DAC}
    checks = [_check_at(song, played, early, melodic, frames, channels) for early in (False, True)]
    return min(checks, key=lambda c: sum(not m.tied for m in c.misses))


def _check_at(song: SmpsSong, played: PlayedSong, early: bool, melodic: set[str], frames: FrameLog,
              channels: ChannelChoice) -> FrameCheck:
    schedule = tempo_schedule(played.modifier, played.tempo_changes, played.tempo_phase - early)
    notes = {name: [(frame_of_tick(schedule, n.tick), n) for n in ns if not n.rest]
             for name, ns in played.channels.items() if name in melodic}
    check = FrameCheck(_offset(notes, frames), early)
    first_steps = {name: env.steps[0] for name, env in song.rules.psg_envelopes.items() if env.steps}
    for name, sounding in notes.items():
        if not channels.picks(name):
            continue
        for frame, note in sounding:
            at = frame - check.offset
            if 0 <= at < len(frames.frames):
                _check_note(check, name, note, frames, at, first_steps)
    return check


def _offset(notes: dict[str, list[tuple[int, PlayedNote]]], frames: FrameLog) -> int:
    """The frames the rip starts into the song: the commonest over the FM channels."""
    found: Counter[int] = Counter()
    for name, sounding in notes.items():
        attacks = [frame for frame, note in sounding if note.attack]
        if name not in FM_CHANNEL_NAMES or not attacks:
            continue
        index = FM_CHANNEL_NAMES.index(name)
        key_on = next((f.index for f in frames.frames if any(f.fm[index].keys)), None)
        if key_on is not None:
            found[attacks[0] - key_on] += 1
    return found.most_common(1)[0][0] if found else 0


def _check_note(check: FrameCheck, name: str, note: PlayedNote, frames: FrameLog, at: int,
                first_steps: dict[str, int]) -> None:
    frame = frames.frames[at]
    if name in FM_CHANNEL_NAMES:
        fm = frame.fm[FM_CHANNEL_NAMES.index(name)]
        got = {FrameAspect.PITCH: freq_word(fm.fnum, fm.block), FrameAspect.LEVEL: tuple(sorted(fm.carrier_tls))}
        levels = note.level if isinstance(note.level, tuple) else ()     # FM: the carriers' TLs
        want = {FrameAspect.PITCH: note.pitch, FrameAspect.LEVEL: tuple(sorted(levels))}
        if isinstance(note.voice, tuple):                                  # FM: (B0, ((register, byte) ...))
            feedback, timbre = note.voice
            want[FrameAspect.VOICE] = _chip_bits(feedback, dict(timbre))
            got[FrameAspect.VOICE] = _chip_bits(fm.feedback_algorithm, {r: fm.operator(r) for r, _ in timbre})
    else:
        index = PSG_CHANNEL_NAMES.index(name)
        noise = note.noise is not None
        tone, level = frame.psg[_NOISE_TONE if noise else index], frame.psg[NOISE_CHANNEL if noise else index]
        got = {FrameAspect.PITCH: max(1, tone.period), FrameAspect.LEVEL: level.attenuation}
        level = note.level
        if isinstance(level, int) and isinstance(note.voice, str) and note.voice in first_steps:
            level = min(PSG_ATT_SILENT, level + first_steps[note.voice])
        want = {FrameAspect.LEVEL: level}
        if not (isinstance(level, int) and level >= PSG_ATT_SILENT):
            want[FrameAspect.PITCH] = note.pitch

    tied = not note.attack
    for aspect, value in want.items():
        check.checked[name, aspect, tied] += 1
        if value != got[aspect]:
            check.misses.append(FrameMiss(name, note.tick, tied, aspect, value, got[aspect]))


def _chip_bits(feedback: int, registers: dict[int, int]) -> tuple[int, dict[int, int]]:
    """A voice as the chip reads it: B0 and each operator register masked to their bits."""
    return feedback & FEEDBACK_ALGORITHM_MASK, {r: operator_bits(r, v) for r, v in registers.items()}
