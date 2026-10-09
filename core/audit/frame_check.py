"""A song against its rip's frame log, note by note and with no lift: the chip's registers on
each note's frame (core.vgm.frames), where the song says the note starts.

The lift (rip_diff.py) reads notes back from the log, so it cannot see a detune, a voice or a slide
that writes no key; this reads what the chip was given, for any driver:

    pitch   FM: block << 11 | fnum, detune in; PSG: the tone divider (noise: tone 3's, 0 read as 1)
    level   FM: the carriers' TL; PSG: the attenuation (noise: the noise channel's)
    voice   FM: the feedback / algorithm and every operator register but the carriers' TL, the bits
            the chip reads (a driver may write B0 with the voice's byte whole)

Attacking notes and tied ones are counted apart: vibrato runs on through a tie, so a tied note's
pitch is the song's and the vibrato so far.  The rip may start late: the offset is the commonest
difference, over the FM channels, between a channel's first attack in the song and its first
key-on in the rip.  A drum track is not read (a DAC sample, or Type 0 FM's FM drums on FM3).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from ..chips import FEEDBACK_ALGORITHM_MASK, REGISTER_MASKS
from ..smps import ChannelType, PlayedNote, SmpsSong, frame_of_tick, played_song, source_map, tempo_schedule
from ..vgm import FrameLog
from .rip_diff import ChannelChoice

_EVERY_CHANNEL = ChannelChoice()
_NOISE_TONE, _NOISE = 2, 3              # the PSG's tone 3 clocks the noise channel
_OPERATOR_FIRST, _GROUP, _SLOT = 0x30, 0x10, 0x04     # a frame's operator bytes: 4 slots per register
_SLOTS = 4
_GROUP_BITS = 0xF0


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
    checked: Counter[tuple[str, FrameAspect, bool]] = field(default_factory=Counter)   # (channel, aspect, tied)
    misses: list[FrameMiss] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every attacking note as the rip has it."""
        return not any(not m.tied for m in self.misses)

    def missed(self) -> Counter[tuple[str, FrameAspect, bool]]:
        return Counter((m.channel, m.aspect, m.tied) for m in self.misses)


def check_frames(song: SmpsSong, frames: FrameLog, channels: ChannelChoice = _EVERY_CHANNEL) -> FrameCheck:
    """Each FM and PSG note `song` plays against `frames` on its frame."""
    played = played_song(song)
    schedule = tempo_schedule(played.modifier, played.tempo_changes, played.tempo_phase)
    melodic = {name for name, ch in source_map(song).items() if ch.header.channel_type != ChannelType.DAC}
    notes = {name: [(frame_of_tick(schedule, n.tick), n) for n in ns if not n.rest]
             for name, ns in played.channels.items() if name in melodic and channels.picks(name)}
    check = FrameCheck(_offset(notes, frames))
    for name, sounding in notes.items():
        for frame, note in sounding:
            at = frame - check.offset
            if 0 <= at < len(frames.frames):
                _check_note(check, name, note, frames, at)
    return check


def _offset(notes: dict[str, list[tuple[int, PlayedNote]]], frames: FrameLog) -> int:
    """The frames the rip starts into the song: the commonest over the FM channels."""
    found: Counter[int] = Counter()
    for name, sounding in notes.items():
        attacks = [frame for frame, note in sounding if note.attack]
        if not name.startswith("FM") or not attacks:
            continue
        index = int(name.removeprefix("FM")) - 1
        key_on = next((f.index for f in frames.frames if any(f.fm[index].keys)), None)
        if key_on is not None:
            found[attacks[0] - key_on] += 1
    return found.most_common(1)[0][0] if found else 0


def _check_note(check: FrameCheck, name: str, note: PlayedNote, frames: FrameLog, at: int) -> None:
    frame = frames.frames[at]
    index = int(name.removeprefix("FM").removeprefix("PSG")) - 1
    if name.startswith("FM"):
        fm = frame.fm[index]
        got = {FrameAspect.PITCH: fm.block << 11 | fm.fnum, FrameAspect.LEVEL: tuple(sorted(fm.carrier_tls))}
        levels = note.level if isinstance(note.level, tuple) else ()     # FM: the carriers' TLs
        want = {FrameAspect.PITCH: note.pitch, FrameAspect.LEVEL: tuple(sorted(levels))}
        if isinstance(note.voice, tuple):                                  # FM: (B0, ((register, byte) ...))
            feedback, timbre = note.voice
            want[FrameAspect.VOICE] = _chip_bits(feedback, dict(timbre))
            got[FrameAspect.VOICE] = _chip_bits(fm.feedback_algorithm, {r: _operator(fm.operators, r) for r, _ in timbre})
    else:
        noise = note.noise is not None
        tone, level = frame.psg[_NOISE_TONE if noise else index], frame.psg[_NOISE if noise else index]
        got = {FrameAspect.PITCH: max(1, tone.period), FrameAspect.LEVEL: level.attenuation}
        want = {FrameAspect.PITCH: note.pitch, FrameAspect.LEVEL: note.level}

    tied = not note.attack
    for aspect, value in want.items():
        check.checked[name, aspect, tied] += 1
        if value != got[aspect]:
            check.misses.append(FrameMiss(name, note.tick, tied, aspect, value, got[aspect]))


def _chip_bits(feedback: int, registers: dict[int, int]) -> tuple[int, dict[int, int]]:
    """A voice as the chip reads it: B0 and each operator register masked to their bits."""
    return feedback & FEEDBACK_ALGORITHM_MASK, {r: v & REGISTER_MASKS[r & _GROUP_BITS] for r, v in registers.items()}


def _operator(operators: bytes, register: int) -> int:
    """A channel-0 operator register's byte in a frame's 28."""
    group, slot = divmod(register - _OPERATOR_FIRST, _GROUP)
    return operators[group * _SLOTS + slot // _SLOT]
