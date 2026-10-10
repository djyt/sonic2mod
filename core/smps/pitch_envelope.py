"""A driver's pitch envelope: from a note's read, one step a frame, each step's offset added to the
note's frequency word (the note's own word, not the last step's sum).  Space Harrier II's
(docs/todo/space_harrier_2.md 2.3): a scoop into the note, then a vibrato whose depth grows each
pass.

    steps      offsets (signed: added to the FM word) and commands, which take no frame:
               Restart   back to step 0
               Jump      to a step
               Scale     the scale grows by n: each offset after it is the offset x (1 + scale)
               Hold      the envelope stops: the word stays where the last step left it

The read frame plays step 0; every read starts again (a tie under legato too).  A driver reads
its bytes into these (its own command bytes); `offsets` plays them.
"""

from __future__ import annotations

from dataclasses import dataclass

_BYTE = 0x100
_COMMANDS_PER_FRAME = 0x100        # past this, an envelope that only loops on commands is refused


@dataclass(frozen=True)
class Restart:
    pass


@dataclass(frozen=True)
class Jump:
    to: int                        # a step index


@dataclass(frozen=True)
class Scale:
    by: int


@dataclass(frozen=True)
class Hold:
    pass


Step = int | Restart | Jump | Scale | Hold


@dataclass(frozen=True)
class PitchEnvelope:
    steps: tuple[Step, ...]

    def offsets(self, frames: int) -> tuple[int, ...]:
        """The offset each of the first `frames` frames from the read adds to the word.  The scale
        multiplies in a byte, as the driver does (an offset's low byte times 1 + scale, its sign
        kept)."""
        out: list[int] = []
        at, scale, last = 0, 0, 0
        while len(out) < frames:
            for _ in range(_COMMANDS_PER_FRAME):
                step = self.steps[at] if at < len(self.steps) else Hold()
                match step:
                    case Restart():
                        at = 0
                    case Jump(to=to):
                        at = to
                    case Scale(by=by):
                        scale = (scale + by) % _BYTE
                        at += 1
                    case Hold():
                        return (*out, *[last] * (frames - len(out)))
                    case int():
                        last = _scaled(step, scale)
                        at += 1
                        break
            else:
                raise ValueError(f"pitch envelope {self.steps}: {_COMMANDS_PER_FRAME} commands without an offset")
            out.append(last)
        return tuple(out)


def _scaled(offset: int, scale: int) -> int:
    """`offset` x (1 + scale) as the driver adds it: the low byte multiplied, the sign kept."""
    low = (offset % _BYTE) * (1 + scale) % _BYTE
    return low - _BYTE if offset < 0 else low
