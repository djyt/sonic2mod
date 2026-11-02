"""A Time Trax song walked as its driver plays it, frame by frame, for what its bytes do not say
(SongReading):

    fixed      $86 n drops the duration byte after each note: a note's length depends on the path
               that reached it
    loop ends  $83 names no start: it closes the $82 on the track's loop stack (3-byte entries:
               the body's address and the passes left, $0E82)
    waits      $8E holds its track until another signals it ($8D n): the frames it waits.  Song 0's
               FM5 waits near the end of each pass ($A2B8) for FM3's signal just before its loop's
               jump ($A01B): FM5's 5508-frame pass stretched to FM3's 6144

Each track is followed with its stacks and counts, a duration a count of frames, to its loop's jump
(the code reached again in the same state) or its stop.  The update runs FM1-FM6 in order each frame
($074A), so a signal reaches a later track in the frame it is sent.  A wait reads `$8E` again every
other frame (its countdown loaded with 1, $0FFA), and goes on at the first read that finds the flag,
clearing it.  A waiting track is walked two passes: its wait must be the same in each.

    FM3   ... $8D 5 | jump ...................... $8D 5 | jump        a signal each 6144 frames
    FM5   ... $8E . . . . (polls) go | jump ... $8E . . . go | jump   read as a rest of the wait
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from core.rom.image import RomError
from core.rom.memory import SoundMemory

from .commands import COMMANDS, FIRST_COMMAND, NO_DURATION, Steer, counted
from .memory import SongReading

_MOST_STEPS = 2_000_000           # a track's walk with no loop or stop in sight
_POLL = 2                         # a wait reads its byte every other frame
_NOTE_AND_DURATION = 2
_WAITING_PASSES = 2               # a waiting track's passes: its waits compared


@dataclass
class _Walked:
    """One track's walk: what it read where, and the signals it sent."""

    fixed: dict[int, int] = field(default_factory=dict)
    loop_ends: dict[int, tuple[int, int]] = field(default_factory=dict)
    waits: defaultdict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    signals: defaultdict[int, list[int]] = field(default_factory=lambda: defaultdict(list))  # track -> frames
    loop: tuple[int, int] | None = None           # (the frame its loop starts at, its length); None: it stops


class _Waits(Exception):
    """A track met a wait before the signals were known."""


@dataclass(frozen=True)
class _Signals:
    """The signals the walked tracks send to track `number`: when each is sent, as (frame, track)
    in the update's order, its loop's repeats included."""

    number: int
    senders: dict[int, _Walked]

    def first_after(self, after: tuple[int, int]) -> tuple[int, int] | None:
        """The first signal sent later than `after`; None: none ever."""
        found = [sent for sender, walked in self.senders.items() for frame in walked.signals.get(self.number, ())
                 if (sent := _next_repeat(frame, sender, walked.loop, after)) is not None]
        return min(found, default=None)


def walk_song(memory: SoundMemory, numbers: dict[int, int]) -> SongReading:
    """Every track (start -> track number) walked: tracks that wait after the others, by their signals."""
    walked: dict[int, _Walked] = {}
    waiting: list[tuple[int, int]] = []
    for start, number in numbers.items():
        try:
            walked[number] = _walk(memory, start, number, None)
        except _Waits:
            waiting.append((start, number))

    senders = dict(walked)
    for start, number in waiting:
        track = _walk(memory, start, number, _Signals(number, senders), _WAITING_PASSES)
        if track.signals:
            raise RomError(f"${start:X}: a track that waits signals too: not read")
        walked[number] = track
    return _reading(walked.values())


def _walk(memory: SoundMemory, start: int, number: int, signals: _Signals | None, passes: int = 1) -> _Walked:
    """One track from `start`, to its stop or its loop's jump taken `passes` times."""
    out = _Walked()
    pc, fixed, frame = start, NO_DURATION, 0
    loops: list[tuple[int, int, int]] = []        # (body, passes, passes left)
    calls: list[int] = []
    seen: dict[tuple, int] = {}                   # the state at each step -> its frame
    consumed = (-1, number)                       # when the wait last cleared the flag
    for _ in range(_MOST_STEPS):
        state = (pc, fixed, tuple(loops), tuple(calls))
        seen.setdefault(state, frame)
        byte = memory.byte(pc)

        # A note or a rest: its duration the fixed one, else its byte
        if byte < FIRST_COMMAND:
            _keep(out.fixed, pc, fixed, "a fixed duration")
            frame += counted(fixed or memory.byte(pc + 1))
            pc += 1 if fixed else _NOTE_AND_DURATION
            continue

        command = COMMANDS.get(byte)
        if command is None:
            raise RomError(f"${pc:X}: ${byte:02X}: past the driver's command table")
        match command.steer:
            case Steer.STOP:
                return out
            case Steer.JUMP:
                pc = memory.code_pointer(pc + 1)
                again = seen.get((pc, fixed, tuple(loops), tuple(calls)))
                if again is not None:
                    out.loop = (again, frame - again)
                    passes -= 1
                    if not passes:
                        return out
                    seen.clear()
            case Steer.LOOP:
                n = counted(memory.byte(pc + 1))
                loops.append((pc + 2, n, n))
                pc += 2
            case Steer.LOOP_END:
                if not loops:
                    raise RomError(f"${pc:X}: a loop's end with no loop open")
                body, n, left = loops.pop()
                _keep(out.loop_ends, pc, (body, n), "a loop")
                if left > 1:
                    loops.append((body, n, left - 1))
                    pc = body
                else:
                    pc += 1
            case Steer.CALL:
                calls.append(pc + 3)
                pc = memory.code_pointer(pc + 1)
            case Steer.RETURN:
                if not calls:
                    raise RomError(f"${pc:X}: a return with no call")
                pc = calls.pop()
            case Steer.FIXED:
                fixed = memory.byte(pc + 1)
                pc += 2
            case Steer.SIGNAL:
                out.signals[memory.byte(pc + 1) - 1].append(frame)
                pc += 2
            case Steer.WAIT:
                if signals is None:
                    raise _Waits
                goes = _goes_on(pc, frame, number, signals, consumed)
                consumed = (goes, number)
                out.waits[pc].append(goes - frame)
                frame = goes
                pc += 1
            case Steer.REFUSED:
                raise RomError(f"${pc:X}: ${byte:02X} ({command.what}): no song plays it, not read")
            case Steer.NONE:
                pc += 1 + command.operands
    raise RomError(f"${start:X}: a track with no loop or stop in {_MOST_STEPS} steps")


def _goes_on(at: int, frame: int, number: int, signals: _Signals, consumed: tuple[int, int]) -> int:
    """The frame a wait read at `frame` goes on at: its first read (every other frame) after a
    signal sent since the flag last cleared."""
    sent = signals.first_after(consumed)
    if sent is None:
        raise RomError(f"${at:X}: a wait no track signals")
    goes = frame
    while (goes, number) < sent:
        goes += _POLL
    return goes


def _next_repeat(frame: int, sender: int, loop: tuple[int, int] | None, after: tuple[int, int]) -> tuple[int, int] | None:
    """The first time the signal sent at `frame` (again each pass of the sender's loop, if within
    it) is sent later than `after`."""
    if (frame, sender) > after:
        return frame, sender
    if loop is None or frame < loop[0] or not loop[1]:
        return None
    length = loop[1]
    passes = (after[0] - frame) // length
    for k in (passes, passes + 1, passes + 2):
        if (frame + k * length, sender) > after:
            return frame + k * length, sender
    return None


def _keep(found: dict, at: int, value: object, what: str) -> None:
    """`value` read at `at`; the same address read otherwise elsewhere is an error."""
    if found.setdefault(at, value) != value:
        raise RomError(f"${at:X}: reached under {what} {found[at]} and {value}: read one way only")


def _reading(tracks) -> SongReading:
    """The tracks' readings as one: code two tracks share read alike, each wait the same each pass."""
    reading = SongReading()
    for walked in tracks:
        for at, value in walked.fixed.items():
            _keep(reading.fixed, at, value, "a fixed duration")
        for at, loop in walked.loop_ends.items():
            _keep(reading.loop_ends, at, loop, "a loop")
        for at, frames in walked.waits.items():
            if len(set(frames)) != 1:
                raise RomError(f"${at:X}: a wait of {sorted(set(frames))} frames by pass: read one way only")
            _keep(reading.waits, at, frames[0], "a wait")
    return reading
