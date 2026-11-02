"""Time Trax's track code: what each byte is and how many operand bytes follow it, as the FM tracks'
command table (Z80 $0DC8, $80-$AB) reads them.  The grammar (grammar.py) says what each plays as;
the song's walk (walk.py) follows the ones that steer it.

    $00        rest       + a duration byte, unless a fixed duration is on ($86)
    $01-$7F    a note     the FM table's entry note + transposition, + a duration byte alike
    $80-$AB    a command  its operand bytes (Command.operands)

A duration counts frames, 1-255; 0 is 256 (the countdown is loaded with it less one).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

FIRST_COMMAND = 0x80
REST_BYTE = 0x00
NO_DURATION = 0                   # a fixed duration of 0: each note carries its own byte
_WHOLE_COUNT = 0x100              # a duration (or a loop count) of 0: the 8-bit counter runs round


class Steer(Enum):
    """What a command does to the walk through the code (NONE: nothing)."""
    NONE = auto()
    STOP = auto()                 # $80
    JUMP = auto()                 # $81 w: absolute, big-endian
    LOOP = auto()                 # $82 n: n passes of the code after it, to its $83
    LOOP_END = auto()             # $83
    CALL = auto()                 # $84 w
    RETURN = auto()               # $85
    FIXED = auto()                # $86 n: every note's duration n, no byte of its own (0: off)
    SIGNAL = auto()               # $8D n: track n (1-8) may go on past its wait
    WAIT = auto()                 # $8E: wait until signalled
    REFUSED = auto()              # no song plays it: not read


@dataclass(frozen=True)
class Command:
    operands: int
    what: str
    steer: Steer = Steer.NONE
    later: bool = False           # read and dropped until the walk plays it (docs/todo/time_trax.md Phase 2)


# The commands the song keeps as effects (grammar.py)
PAN, VOICE, TRANSPOSE, RE_KEY, LEGATO, DETUNE, DETUNE_OFF = 0x87, 0x88, 0x89, 0x8B, 0x8C, 0xA8, 0xA9
GATE, GATE_OFF, KEY_OFF_AFTER, KEY_OFF_AFTER_OFF = 0x96, 0x97, 0x98, 0x99   # each set clears the other's
VOLUME, MASK, MASK_CARRIERS = 0x8A, 0xAA, 0xAB
GLIDE, GLIDE_OFF, PORTAMENTO, PORTAMENTO_OFF = 0x92, 0x93, 0x94, 0x95


COMMANDS: dict[int, Command] = {
    0x80: Command(0, "stop", Steer.STOP),
    0x81: Command(2, "jump", Steer.JUMP),
    0x82: Command(1, "loop", Steer.LOOP),
    0x83: Command(0, "loop end", Steer.LOOP_END),
    0x84: Command(2, "call", Steer.CALL),
    0x85: Command(0, "return", Steer.RETURN),
    0x86: Command(1, "fixed duration", Steer.FIXED),
    PAN: Command(1, "pan"),
    VOICE: Command(1, "voice"),
    TRANSPOSE: Command(1, "transposition"),
    VOLUME: Command(1, "volume"),
    RE_KEY: Command(0, "notes re-key"),
    LEGATO: Command(0, "legato"),
    0x8D: Command(1, "signal", Steer.SIGNAL),
    0x8E: Command(0, "wait", Steer.WAIT),
    0x8F: Command(2, "jump unless signalled", Steer.REFUSED),
    0x90: Command(0, "loop end unless signalled", Steer.REFUSED),
    0x91: Command(1, "random jump", Steer.REFUSED),           # n, then n words: refused before they matter
    GLIDE: Command(2, "glide"),
    GLIDE_OFF: Command(0, "glide off"),
    PORTAMENTO: Command(2, "portamento"),
    PORTAMENTO_OFF: Command(0, "portamento off"),
    GATE: Command(1, "key off before the end"),
    GATE_OFF: Command(0, "key off before the end off"),
    KEY_OFF_AFTER: Command(1, "key off after the start"),
    KEY_OFF_AFTER_OFF: Command(0, "key off after the start off"),
    0x9A: Command(6, "vibrato", later=True),
    0x9B: Command(0, "vibrato restart", later=True),
    0x9C: Command(0, "vibrato off", later=True),
    0x9D: Command(0, "portamento ignores transposition", Steer.REFUSED),
    0x9E: Command(0, "portamento follows transposition", Steer.REFUSED),
    0x9F: Command(2, "volume envelopes", later=True),
    0xA0: Command(0, "volume envelopes on", later=True),
    0xA1: Command(0, "volume envelopes off", later=True),
    0xA2: Command(1, "release envelope before the end", Steer.REFUSED),
    0xA3: Command(0, "release envelope before the end off", Steer.REFUSED),
    0xA4: Command(1, "release envelope after the start", Steer.REFUSED),
    0xA5: Command(0, "release envelope after the start off", Steer.REFUSED),
    0xA6: Command(1, "LFO", Steer.REFUSED),
    0xA7: Command(1, "DAC sample", Steer.REFUSED),
    DETUNE: Command(1, "detune"),
    DETUNE_OFF: Command(0, "detune off"),
    MASK: Command(1, "volume mask"),
    MASK_CARRIERS: Command(0, "volume mask: the carriers"),
}


def counted(byte: int) -> int:
    """A duration byte's frames, or a loop byte's passes: 0 is 256."""
    return byte or _WHOLE_COUNT
