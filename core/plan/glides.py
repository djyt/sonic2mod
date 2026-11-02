"""Glides, portamento and volume envelopes on the MOD (docs/todo/time_trax.md D4, D5): what a channel's
driver writes frame by frame after each read, and whether a sample can follow it.  A pitch curve the
MOD can follow slides row by row, a level moves by Cxx (core.convert's PitchCurve, LevelCurve); a
sweep it cannot follow, and an envelope's level, are baked into a sample of their own where a slot
is free (core.plan.baked).

    channel_reads(channel, timeline)   each note and rest of a channel: its word on each frame, the
                                       TLs it reads at and those after (LevelPath)
    reaches(words, index, chip, ...)   the sample at MOD note `index` plays every word within B-3 .. C-1

A curve runs from a read through the ties after it (held rests: the sample plays on through them).
A legato read the sounding sample can follow glides on (no key-on); any other read keys a sample.

    read C (key-on)  legato read E (glides on)  tie  |  read E0 (key-on: a sweep down 4 octaves)  tie
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..chips import freq_word_hz
from ..mod import PERIOD_TABLE
from ..smps import (
    LevelPath,
    PitchSlide,
    SmpsChannel,
    SmpsEvent,
    SmpsVoice,
    TrackState,
    fm_render_word,
)
from .timeline import Timeline

_CENTS_PER_OCTAVE = 1200
_LOWEST, _HIGHEST = min(p for p in PERIOD_TABLE if p), max(PERIOD_TABLE)    # B-3 .. C-1: what the MOD plays
_TL = 0x40
_TL_END = 0x50                  # channel 0's TL registers: $40 .. $4C

TLs = tuple[tuple[int, int], ...]               # (register, TL), channel 0's


@dataclass(frozen=True)
class Read:
    """A note or rest as its driver plays it frame by frame."""
    event: SmpsEvent
    frames: int
    words: tuple[int, ...]                      # the word written each frame (none: its driver slides nothing)
    tls: TLs = ()                               # the TLs it reads at (none: no voice yet)
    path: tuple[tuple[int, TLs], ...] = ()      # (frame, TLs) at each change after (LevelPath)

    def levels(self) -> tuple[TLs, ...]:
        """The TLs each frame."""
        changes, out, now = dict(self.path), [], self.tls
        for frame in range(self.frames):
            now = changes.get(frame, now)
            out.append(now)
        return tuple(out)


Walked = list[Read]


def channel_reads(channel: SmpsChannel, timeline: Timeline, voices: dict[int, SmpsVoice]) -> Walked | None:
    """Each note and rest of the channel's own (a merged build's spliced ones aside) as its driver
    plays it: the driver steps through both.  None where its driver neither slides nor runs volume
    envelopes."""
    rules = channel.rules.track(channel.header.channel_type)
    slide = PitchSlide.for_channel(channel)
    if slide is None and rules.volume.envelopes is None:
        return None
    st, out, path = TrackState.for_channel(channel), [], ()
    for event in channel.events:
        if event.spliced is not None:
            continue
        if event.effect is not None:
            st.apply(event.effect)
            if slide is not None:
                slide.apply(event.effect, st.transpose)
            if isinstance(event.effect, LevelPath):
                path = event.effect.steps
            continue
        if event.note is None:
            continue
        frames = max(1, round(event.note.duration / timeline.ticks_per_frame_at(event.tick_position)))
        words = slide.frames(event.note, frames, st.transpose, st.detune) if slide is not None else ()
        voice = voices.get(st.voice) if st.voice is not None else None
        tls = () if voice is None else tuple((r, v) for r, v in sorted(voice.chip_registers(st.tl).items())
                                             if _TL <= r < _TL_END)
        out.append(Read(event, frames, words, tls, path))
        path = ()
    return out


def with_ties(walked: Walked, i: int) -> list[Read]:
    """Read `i` and the held rests after it: what the sample it keys plays on through."""
    reads = [walked[i]]
    for read in walked[i + 1:]:
        note = read.event.note
        if note is None or not (note.is_rest and note.is_no_attack):
            break
        reads.append(read)
    return reads


def word_period(word: int, index: int, chip: int, fm_frequencies: tuple[int, ...], cents: float = 0.0) -> float:
    """The period `word` plays at on the sample at MOD note `index`, which plays `chip` (SMPS
    semitone) at its table word, `cents` off it (the sample's own detune)."""
    base = fm_render_word(chip, fm_frequencies)
    moved = _CENTS_PER_OCTAVE * math.log2(freq_word_hz(word) / freq_word_hz(base)) - cents
    return PERIOD_TABLE[index] * 2 ** (-moved / _CENTS_PER_OCTAVE)


def reaches(words: tuple[int, ...], index: int, chip: int, fm_frequencies: tuple[int, ...], cents: float = 0.0) -> bool:
    """The sample at MOD note `index` (playing `chip`, `cents` off) plays every word within B-3 .. C-1."""
    return all(_LOWEST <= word_period(w, index, chip, fm_frequencies, cents) <= _HIGHEST for w in set(words))


