"""A coordination flag as a song keeps it: one frozen class per meaning, its operands named.

    SetVoice(index=3)    ModSet(wait=1, speed=1, delta=4, steps=2)    PsgVoice(envelope="fTone_01")

Each driver maps its own bytes to these (core/drivers: Sonic 1's are s1.sounddriver.asm
coordflagLookup, docs/smps_driver.md; the Sonic 1 byte noted below); the SMPS2ASM macro names are
core/smps/names.py's, for reading and printing assembly.  Read an effect by matching its class:

    match effect:
        case SetVoice(index=i): ...

`values` gives the operands in order (what the asm writer prints); effect_of(flag, values) builds
one from a flag and its operands.

Two kinds.  A PlayedEffect is one a song's events keep (SmpsEvent.effect).  A DriverEffect says
what one driver does in its own terms; the walk resolves each into what it plays as, by the
song's PlaybackRules (driver_track.py), so no event can keep one:

    VolumeStep, AlterVolumeStep -> SetVol        DetuneAdd -> Detune
    Gate -> each note after it cut short, a rest after it
    VoiceRegister -> SetVoice of a patched copy (voice_patch.py)
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum, auto
from typing import ClassVar

_PAN_SPEAKERS = 0xC0              # B4 bits 7 (left) and 6 (right)
_PAN_LEFT = 0x80
_PAN_RIGHT = 0x40


class CoordFlag(StrEnum):
    """What a coordination flag does: a meaning, no driver's byte."""

    PAN = auto()                  # Sonic 1 $E0
    DETUNE = auto()               # $E1
    NOP = auto()                  # $E2
    CHAN_TEMPO_DIV = auto()       # $E5
    ALTER_VOL = auto()            # $E6 FM, $EC PSG
    NOTE_FILL = auto()            # $E8
    CHANGE_TRANSPOSITION = auto() # $E9
    SET_TEMPO_MOD = auto()        # $EA
    SET_TEMPO_DIV = auto()        # $EB
    SET_VOICE = auto()            # $EF
    MOD_SET = auto()              # $F0
    MOD_ON = auto()               # $F1
    PSG_FORM = auto()             # $F3
    MOD_OFF = auto()              # $F4
    PSG_VOICE = auto()            # $F5
    SET_VOL = auto()              # Type 0 FM's $F0
    DAC_SAMPLE = auto()           # Streets of Rage's $F0
    VOLUME_STEP = auto()          # Streets of Rage's $F1
    ALTER_VOLUME_STEP = auto()    # its $FB on FM
    DETUNE_ADD = auto()           # its $F2 with a third byte
    GATE = auto()                 # its $F3
    VOICE_REGISTER = auto()       # its $FA on FM


@dataclass(frozen=True)
class SmpsEffect:
    """A coordination flag as read; each class below PlayedEffect and DriverEffect one CoordFlag."""

    flag: ClassVar[CoordFlag]

    @property
    def values(self) -> tuple:
        """The operands in order: SetVoice(3) -> (3,)."""
        return tuple(getattr(self, f.name) for f in fields(self))


@dataclass(frozen=True)
class PlayedEffect(SmpsEffect):
    """A flag a song's events keep: what plays."""


@dataclass(frozen=True)
class DriverEffect(SmpsEffect):
    """A flag in one driver's own terms: the walk resolves it, no event keeps one."""


@dataclass(frozen=True)
class Pan(PlayedEffect):
    flag = CoordFlag.PAN
    b4: int                       # the YM2612 B4 byte: L R AMS FMS

    @property
    def side(self) -> str:
        return pan_side(self.b4)


@dataclass(frozen=True)
class Detune(PlayedEffect):
    flag = CoordFlag.DETUNE
    offset: int                   # added to the frequency word (PSG: the divider); signed


@dataclass(frozen=True)
class Nop(PlayedEffect):
    flag = CoordFlag.NOP
    byte: int


@dataclass(frozen=True)
class ChanTempoDiv(PlayedEffect):
    flag = CoordFlag.CHAN_TEMPO_DIV
    divider: int


@dataclass(frozen=True)
class AlterVol(PlayedEffect):
    flag = CoordFlag.ALTER_VOL
    delta: int                    # signed


@dataclass(frozen=True)
class NoteFill(PlayedEffect):
    flag = CoordFlag.NOTE_FILL
    frames: int


@dataclass(frozen=True)
class ChangeTransposition(PlayedEffect):
    flag = CoordFlag.CHANGE_TRANSPOSITION
    semitones: int                # signed


@dataclass(frozen=True)
class SetTempoMod(PlayedEffect):
    flag = CoordFlag.SET_TEMPO_MOD
    modifier: int


@dataclass(frozen=True)
class SetTempoDiv(PlayedEffect):
    flag = CoordFlag.SET_TEMPO_DIV
    divider: int


@dataclass(frozen=True)
class SetVoice(PlayedEffect):
    flag = CoordFlag.SET_VOICE
    index: int


@dataclass(frozen=True)
class ModSet(PlayedEffect):
    flag = CoordFlag.MOD_SET
    wait: int                     # frames before the first step
    speed: int                    # frames per step
    delta: int                    # added per step (signed on the chip)
    steps: int                    # steps per half cycle


@dataclass(frozen=True)
class ModOn(PlayedEffect):
    flag = CoordFlag.MOD_ON


@dataclass(frozen=True)
class ModOff(PlayedEffect):
    flag = CoordFlag.MOD_OFF


@dataclass(frozen=True)
class PsgForm(PlayedEffect):
    flag = CoordFlag.PSG_FORM
    noise: int                    # the noise register byte


@dataclass(frozen=True)
class PsgVoice(PlayedEffect):
    flag = CoordFlag.PSG_VOICE
    envelope: str                 # fTone_01 ... : the driver's envelope by name


@dataclass(frozen=True)
class SetVol(PlayedEffect):
    flag = CoordFlag.SET_VOL
    level: int                    # the track's volume, absolute


@dataclass(frozen=True)
class SelectSample(PlayedEffect):
    flag = CoordFlag.DAC_SAMPLE
    sound: int                    # the DAC byte the drum track's notes play


@dataclass(frozen=True)
class VolumeStep(DriverEffect):
    """The track's volume as a step of its driver's table (TrackRules.volume_steps), the
    header volume added: the walk's SetVol."""
    flag = CoordFlag.VOLUME_STEP
    step: int


@dataclass(frozen=True)
class AlterVolumeStep(DriverEffect):
    """The track's volume step moved: the walk's SetVol."""
    flag = CoordFlag.ALTER_VOLUME_STEP
    delta: int                    # signed


@dataclass(frozen=True)
class DetuneAdd(DriverEffect):
    """Added to the track's detune word: the walk's Detune."""
    flag = CoordFlag.DETUNE_ADD
    offset: int                   # signed


@dataclass(frozen=True)
class Gate(DriverEffect):
    """Each note keyed off this many frames (track updates) before its end; 0: none."""
    flag = CoordFlag.GATE
    frames: int


@dataclass(frozen=True)
class VoiceRegister(DriverEffect):
    """A YM2612 operator register written over the track's voice until the next voice set."""
    flag = CoordFlag.VOICE_REGISTER
    register: int                 # as the track writes it: its channel's number in the low bits
    value: int


_BY_FLAG: dict[CoordFlag, type[SmpsEffect]] = {
    cls.flag: cls for kind in (PlayedEffect, DriverEffect) for cls in kind.__subclasses__()}
assert set(_BY_FLAG) == set(CoordFlag), "a CoordFlag without its effect class"


def effect_of(flag: CoordFlag, values) -> SmpsEffect:
    """The effect `flag` names, its operands `values` in order."""
    return _BY_FLAG[flag](*values)


def pan_side(b4: int) -> str:
    """The speaker a B4 byte sends the channel to: "L", "R", or "C" for both (or neither, which
    the driver never writes for music)."""
    return {_PAN_LEFT: "L", _PAN_RIGHT: "R"}.get(b4 & _PAN_SPEAKERS, "C")


def pan_is_hard(b4: int) -> bool:
    """True for a channel panned hard left or right."""
    return pan_side(b4) != "C"
