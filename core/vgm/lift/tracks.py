"""What each track did when, read off the frame log: its hits (a note, a tie or a rest) by frame.

The driver writes a channel only when a track reads its data, or a frame effect runs:

    FM      every note read writes a key-on: after a key-off an attack, to a keyed channel a tie
            or a legato note (smpsNoAttack only skips the key-off).  A rest writes a key-off,
            and so does smpsNoteFill, on any frame.
    PSG     the driver writes a note's period and attenuation every frame, but a rip logs a PSG
            write only where the value changes: a note shows where the pitch jumps (more than
            vibrato) or the envelope restarts (the level rises), a rest where it falls silent.
            The noise track's period goes to tone channel 3.  A re-attack at the same pitch and
            level writes nothing - and sounds like nothing either.
    DAC     the Z80 starts a sample when it gets to the 68k's request: a seek belongs to the
            frame whose burst it follows.

Pitch here is the nearest table note (pitch_offset 0); detune and voices come later (1.3-1.8).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator
from dataclasses import dataclass

from ...chips import MD_FM_CLOCK, MD_PSG_CLOCK, fm_frequency_hz, freq_word_hz, psg_frequency_hz
from ..chipstate import FM_CHANNELS, NOISE_CHANNEL, PSG_SILENT
from ..frames import Frame, FrameLog
from ..notes import DEFAULT_MOD_CENTS

_REST = 0x80                    # the note byte of a rest
_FM_FIRST_NOTE = 0x81           # FMSetFreq subtracts $80: table entry 0 is the rest
_PSG_FIRST_NOTE = 0x81          # PSGSetFreq subtracts $81
_CENTS_PER_OCTAVE = 1200.0
_NOISE_TONE = 2                 # the noise track writes its period into tone channel 3


@dataclass(frozen=True, slots=True)
class Hit:
    """A track read: a note (attack or not), a rest, or a DAC sample, on a frame."""

    frame: int
    note: int = _REST           # the SMPS note byte; _REST for a rest
    attack: bool = True
    dac: str = ""

    @property
    def rest(self) -> bool:
        return self.note == _REST and not self.dac


# --- FM -------------------------------------------------------------------------------------


def fm_hits(fl: FrameLog, ch: int, fm_frequencies: tuple[int, ...]) -> list[Hit]:
    """FM channel `ch`'s key writes: a note at each key-on, a rest at each key-off of a keyed
    channel.  A note is the entry of `fm_frequencies` (the driver's table) nearest its pitch."""
    table_hz = [_word_hz(word) for word in fm_frequencies]
    hits = []
    keyed = False
    for frame in fl.frames:
        fm = frame.fm[ch]
        if not fm.keys:
            continue

        # The writes in order: a key-on after a key-off (or to an idle channel) attacks
        attack = on = False
        was_keyed = keyed
        for slots in fm.keys:
            if slots:
                attack |= not keyed
                on = keyed = True
            else:
                keyed = False
        if on:
            hits.append(Hit(frame.index, _fm_note(fm.fnum, fm.block, table_hz), attack))
        elif was_keyed:
            hits.append(Hit(frame.index))
    return hits


def _fm_note(fnum: int, block: int, table_hz: list[float]) -> int:
    """The note byte whose table entry (`table_hz`: each entry's pitch) is nearest the pitch
    (pitch_offset 0)."""
    hz = fm_frequency_hz(fnum, block, MD_FM_CLOCK)
    if hz <= 0:
        return _FM_FIRST_NOTE
    index = min(range(1, len(table_hz)), key=lambda i: abs(math.log2(hz / table_hz[i])))
    return _FM_FIRST_NOTE - 1 + index


def _word_hz(word: int) -> float:
    return freq_word_hz(word)


# --- PSG ------------------------------------------------------------------------------------


def noise_mode(fl: FrameLog) -> bool:
    """Whether PSG3 is a noise track: the noise channel is ever audible."""
    return any(f.psg[NOISE_CHANNEL].attenuation < PSG_SILENT for f in fl.frames)


def psg_hits(fl: FrameLog, ch: int, psg_frequencies: tuple[int, ...], noise: bool = False) -> list[Hit]:
    """PSG track `ch`'s reads: a note at each period write that is not modulation, a rest where
    the channel is silenced without one.  `noise`: PSG3 in noise mode (its period goes to tone
    channel 3, its attenuation to the noise channel)."""
    period_ch, level_ch = (_NOISE_TONE, NOISE_CHANNEL) if noise else (ch, ch)
    hits = []
    before: Frame | None = None
    for frame in fl.frames:
        tone, level = frame.psg[period_ch], frame.psg[level_ch]
        prev_level = before.psg[level_ch].attenuation if before else PSG_SILENT
        prev_period = before.psg[period_ch].period if before else 0
        attack = bool(level.attenuations) and level.attenuations[0] < PSG_SILENT and (
            prev_level >= PSG_SILENT or level.attenuations[0] < prev_level)

        # A note where the level rises, or the period moves unlike vibrato (a write next to
        # another is modulation unless it jumps)
        written_before = before is not None and before.psg[period_ch].period_writes
        moved = tone.period_writes and (not written_before or _jumps(prev_period, tone.period))
        if attack or moved:
            hits.append(Hit(frame.index, _psg_note(tone.period, psg_frequencies), attack))
        elif level.attenuations and level.attenuation >= PSG_SILENT and prev_level < PSG_SILENT:
            hits.append(Hit(frame.index))
        before = frame
    return hits


def _jumps(before: int, after: int) -> bool:
    """A period change wider than vibrato."""
    if before <= 0 or after <= 0:
        return before != after
    return abs(_CENTS_PER_OCTAVE * math.log2(before / after)) > DEFAULT_MOD_CENTS


def _psg_note(period: int, psg_frequencies: tuple[int, ...]) -> int:
    """The note byte whose divider in the driver's table is nearest the period."""
    if period <= 0:
        return _PSG_FIRST_NOTE
    hz = psg_frequency_hz(period, MD_PSG_CLOCK)
    index = min(range(len(psg_frequencies)),
                key=lambda i: abs(math.log2(hz / psg_frequency_hz(max(psg_frequencies[i], 1), MD_PSG_CLOCK))))
    return _PSG_FIRST_NOTE + index


def psg_period_frames(fl: FrameLog, ch: int) -> list[int]:
    """The frames tone channel `ch`'s period is written on, where no two are adjacent (no
    modulation): every write a track read, on a tick.  Empty otherwise."""
    frames = [f.index for f in fl.frames if f.psg[ch].period_writes]
    if any(b - a < 2 for a, b in itertools.pairwise(frames)):
        return []
    return frames


# --- DAC ------------------------------------------------------------------------------------


def dac_hits(fl: FrameLog) -> list[Hit]:
    """Every sample the Z80 started, on the frame that asked for it."""
    return [Hit(fl.burst_frame(sample), dac=f"pcm {offset:#06x}")
            for frame in fl.frames for offset, sample in zip(frame.dac.seeks, frame.dac.seek_samples, strict=True)]


def dac_used(fl: FrameLog) -> bool:
    return any(f.dac_enabled for f in fl.frames)


def fm_key_on_frames(fl: FrameLog) -> Iterator[tuple[int, list[int]]]:
    """(channel, the frames it writes a key-on on), for every FM channel that does: every one a
    track read, on a tick (a key-off may be smpsNoteFill's, on any frame)."""
    for ch in range(FM_CHANNELS):
        frames = [f.index for f in fl.frames if any(f.fm[ch].keys)]
        if frames:
            yield ch, frames

