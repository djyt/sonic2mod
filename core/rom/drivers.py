"""The SMPS 68k drivers a ROM is read with: what each coordination flag byte does and how many
operand bytes follow it.  Everything else these drivers share (headers, relative pointers, the
25-byte voice, the TempoWait tempo) lives in the readers.

    Sonic 1 (SMPS 68k Type 1b, modified)      s1.sounddriver.asm coordflagLookup
    Type 1a (Michael Jackson's Moonwalker)    its jump table at $61290, read with a disassembler

The same byte can mean different things: $F9 is Type 1a's return and Sonic 1's FM1 release rate,
$FB / $FA Type 1a's transposition / tempo divider, Sonic 1's $E9 / $E5.  docs/todo/binary_import.md
has the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from ..smps import SMPS_DAC_NAMES, CoordFlag, SmpsDriver


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


def _effect(flag: CoordFlag, operands: int = 1) -> FlagSpec:
    return FlagSpec(FlagKind.EFFECT, operands, flag)


def _drop(what: str, operands: int = 0, more_if_set: int = 0) -> FlagSpec:
    return FlagSpec(FlagKind.DROP, operands, more_if_set=more_if_set, what=what)


def _refuse(what: str, operands: int = 0) -> FlagSpec:
    return FlagSpec(FlagKind.REFUSE, operands, what=what)


_STOP = FlagSpec(FlagKind.STOP)
_RETURN = FlagSpec(FlagKind.RETURN)
_JUMP = FlagSpec(FlagKind.JUMP, 2)
_LOOP = FlagSpec(FlagKind.LOOP, 4)           # index, count, pointer
_CALL = FlagSpec(FlagKind.CALL, 2)
_NO_ATTACK = FlagSpec(FlagKind.NO_ATTACK)

# Flags Sonic 1 and Type 1a share byte for byte
_COMMON: dict[int, FlagSpec] = {
    0xE0: _effect(CoordFlag.PAN),
    0xE1: _effect(CoordFlag.DETUNE),
    0xE2: _effect(CoordFlag.NOP),
    0xE6: _effect(CoordFlag.ALTER_VOL),
    0xE7: _NO_ATTACK,
    0xE8: _effect(CoordFlag.NOTE_FILL),
    0xEC: _effect(CoordFlag.ALTER_VOL),      # the PSG twin of $E6: one flag in the song
    0xEF: _effect(CoordFlag.SET_VOICE),
    0xF0: _effect(CoordFlag.MOD_SET, 4),
    0xF1: _effect(CoordFlag.MOD_ON, 0),
    0xF2: _STOP,
    0xF3: _effect(CoordFlag.PSG_FORM),
    0xF4: _effect(CoordFlag.MOD_OFF, 0),
    0xF5: _effect(CoordFlag.PSG_VOICE),
    0xF6: _JUMP,
    0xF7: _LOOP,
    0xF8: _CALL,
}


@dataclass(frozen=True)
class RomDriver:
    name: SmpsDriver
    flags: dict[int, FlagSpec]
    envelope_commands: dict[int, EnvelopeCommand]
    dac_names: dict[int, str]             # the DAC track's bytes that play a sample, by name


_LAST_NOTE = 0xDF
# Type 1a sends every note byte to the DAC (the 68k remaps $88-$97 to pitched samples)
_EVERY_DAC_BYTE = {b: f"dac{b:02X}" for b in range(0x81, _LAST_NOTE + 1)}


SONIC1 = RomDriver(SmpsDriver.SONIC1, {
    **_COMMON,
    0xE3: _RETURN,
    0xE4: _STOP,                                   # smpsFade: the 1-Up jingle restores the song
    0xE5: _effect(CoordFlag.CHAN_TEMPO_DIV),
    0xE9: _effect(CoordFlag.CHANGE_TRANSPOSITION),
    0xEA: _effect(CoordFlag.SET_TEMPO_MOD),
    0xEB: _effect(CoordFlag.SET_TEMPO_DIV),
    0xED: _drop("smpsClearPush"),
    0xEE: _STOP,                                   # smpsStopSpecial: FM4 handed back to the music
    0xF9: _drop("smpsMaxRelRate"),
}, {0x80: EnvelopeCommand.HOLD}, {v: k for k, v in SMPS_DAC_NAMES.items()})

TYPE1A = RomDriver(SmpsDriver.TYPE1A, {
    **_COMMON,
    0xE3: _refuse("sets a global flag ($FC clears it; what reads it is not known)"),
    0xE4: _drop("pan animation", 1, more_if_set=4),
    0xE5: _refuse("FM and PSG volume in one flag", 2),
    0xE9: _refuse("LFO", 2),
    0xEA: _effect(CoordFlag.DETUNE),
    0xEB: _drop("queued sound (not part of the music)", 1),
    0xED: _effect(CoordFlag.DETUNE),
    0xEE: _effect(CoordFlag.DETUNE),
    0xF9: _RETURN,
    0xFA: _effect(CoordFlag.CHAN_TEMPO_DIV),
    0xFB: _effect(CoordFlag.CHANGE_TRANSPOSITION),
    0xFC: _refuse("clears $E3's global flag"),
    0xFD: _refuse("SSG-EG", 4),
    0xFE: _refuse("FM3 special mode", 8),
    0xFF: _effect(CoordFlag.PAN),                  # past the jump table: runs into $E0's handler
}, {0x83: EnvelopeCommand.HOLD, 0x80: EnvelopeCommand.RESTART, 0x85: EnvelopeCommand.JUMP},    # $61152
   _EVERY_DAC_BYTE)

DRIVERS: dict[SmpsDriver, RomDriver] = {d.name: d for d in (SONIC1, TYPE1A)}
