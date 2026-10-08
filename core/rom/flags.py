"""The words a driver's flag table is written in: what a coordination flag byte does and how many
operand bytes follow it.  Each variant's table is its family's (smps68k/ ...)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

from ..smps import ChannelType, CoordFlag


class EnvelopeCommand(Enum):
    """What a PSG envelope byte from $80 does (PSGDoVolFX)."""
    HOLD = auto()        # the last step stays
    RESTART = auto()     # back to the first step, this frame
    JUMP = auto()        # to the step the next byte names, this frame


class FlagKind(Enum):
    EFFECT = auto()      # a flag the song keeps as an event (`flag`)
    NO_ATTACK = auto()   # smpsNoAttack: a track byte the walk reads like a note
    RETURN = auto()
    STOP = auto()        # the track ends (smpsStop, and flags that end it as smpsStop does)
    JUMP = auto()
    LOOP = auto()
    CALL = auto()
    DROP = auto()        # decoded for its length, no event: nothing in the MOD stands for it
    REFUSE = auto()      # the converter cannot render it, or what it does is not known


@dataclass(frozen=True)
class FlagSpec:
    kind: FlagKind
    operands: int = 0
    flag: CoordFlag | None = None       # EFFECT: the event
    more_if_set: int = 0                # operand bytes that follow when the first is not 0
    what: str = ""                      # DROP / REFUSE: named in reports and errors


def effect(flag: CoordFlag, operands: int = 1) -> FlagSpec:
    return FlagSpec(FlagKind.EFFECT, operands, flag)


def drop(what: str, operands: int = 0, more_if_set: int = 0) -> FlagSpec:
    return FlagSpec(FlagKind.DROP, operands, more_if_set=more_if_set, what=what)


def refuse(what: str, operands: int = 0) -> FlagSpec:
    return FlagSpec(FlagKind.REFUSE, operands, what=what)


STOP = FlagSpec(FlagKind.STOP)
RETURN = FlagSpec(FlagKind.RETURN)
JUMP = FlagSpec(FlagKind.JUMP, 2)
LOOP = FlagSpec(FlagKind.LOOP, 4)           # index, count, pointer
CALL = FlagSpec(FlagKind.CALL, 2)
NO_ATTACK = FlagSpec(FlagKind.NO_ATTACK)


def every_kind(table: Mapping[int, FlagSpec]) -> dict[ChannelType, Mapping[int, FlagSpec]]:
    """One flag table for every kind of track (DAC, FM, PSG): each SMPS variant so far."""
    return dict.fromkeys(ChannelType, table)
