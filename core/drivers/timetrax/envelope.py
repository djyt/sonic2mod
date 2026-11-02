"""Time Trax's volume envelopes, VolumeEnvelopes: a table of each one's big-endian address
(DriverTables.envelopes), each a start level and segments, played by $1421 a frame at a time.

    start  (frames level) ...  $FF   hold the last level        envelope 0: 230 (192 200) (64 245) $FF
                               $FE   back to the first segment

A segment's first frame sets the volume to where the last ended (the start level, first); then the
volume steps by 1 toward its level, Bresenham's way: over `frames` frames where it moves less than
that ($14E2), else `frames` frames of several steps ($1509):

    slow (frames >= rise)   each frame  err += rise;    err >= frames: err -= frames, one step
    fast (frames < rise)    each frame  steps while err + frames < rise: err += frames; then one
                                        more, err += frames - rise          (err from half the count)

A segment of `frames` frames takes frames + 1 (its first sets the level), and ends at its level
exactly only where its steps run out: the next segment's first frame sets that level.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.rom.memory import SoundMemory

_HOLD, _LOOP = 0xFF, 0xFE
_BYTE = 0xFF
_CARRY = 0x100
_WORD = 2
_SEGMENT = 2                    # frames, level


@dataclass(frozen=True)
class Envelope:
    start: int
    segments: tuple[tuple[int, int], ...]    # (frames, level)
    loops: bool                              # $FE: back to the first segment; else $FF: holds


@dataclass(frozen=True)
class TimeTraxEnvelopes:
    envelopes: tuple[Envelope, ...]

    def start(self, index: int) -> _Run:
        """Envelope `index` as a note read loads it ($153F)."""
        return _Run(self.envelopes[index])


def read_envelopes(memory: SoundMemory, table: int) -> TimeTraxEnvelopes:
    """The table at `table`: an address per envelope, up to the first envelope's bytes."""
    envelopes, end, at = [], None, table
    while end is None or at < end:
        address = memory.header_pointer(table, at)
        end = address if end is None else min(end, address)
        envelopes.append(_envelope(memory, address))
        at += _WORD
    return TimeTraxEnvelopes(tuple(envelopes))


def _envelope(memory: SoundMemory, address: int) -> Envelope:
    segments, at = [], address + 1
    while memory.byte(at) not in (_HOLD, _LOOP):
        segments.append((memory.byte(at), memory.byte(at + 1)))
        at += _SEGMENT
    return Envelope(memory.byte(address), tuple(segments), memory.byte(at) == _LOOP)


class _Run:
    """One envelope playing: the driver's track bytes $4E-$54 as fields."""

    def __init__(self, envelope: Envelope) -> None:
        self._envelope = envelope
        self._at = 0                # the next segment
        self._level = envelope.start   # where the segment heads ($50)
        self._count = 0             # its steps left ($53); 0: the next frame starts a segment
        self._error = 0             # $52
        self._add = 0               # what err gains a frame, wrapped in a byte ($51 slow / $4E fast)
        self._back = 0              # ... and where it carries ($4E slow / $51 fast)
        self._fast = False
        self._way = 1
        self.done = False

    def frame(self, volume: int) -> int:
        if self._count:
            return self._step(volume)
        return self._segment()

    def _segment(self) -> int:
        """A segment's first frame: the volume where the last one headed; the next set up."""
        start = self._level
        if self._at == len(self._envelope.segments):
            if not self._envelope.loops or not self._envelope.segments:
                self.done = True
                return start
            self._at = 0
        frames, self._level = self._envelope.segments[self._at]
        self._at += 1

        rise = abs(self._level - start)
        self._way = -1 if self._level < start else 1
        self._fast = frames < rise
        if self._fast:
            self._count, self._add, self._back = rise, (frames - rise) & _BYTE, rise
        else:
            self._count, self._add, self._back = frames, (rise - frames) & _BYTE, frames
        self._error = self._count >> 1
        return start

    def _step(self, volume: int) -> int:
        """A ramp's frame: slow, one step at most; fast, steps until err carries, then one more."""
        while True:
            total = self._error + self._add
            if total >= _CARRY:
                self._error = total & _BYTE
                self._count -= 1
                return (volume + self._way) & _BYTE
            self._error = (total + self._back) & _BYTE
            self._count -= 1
            if not self._fast:
                return volume
            volume = (volume + self._way) & _BYTE
            if not self._count:
                return volume
