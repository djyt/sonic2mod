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
    Legato -> each note after it tied, as smpsNoAttack before it would
    VoiceRegister, Fm3Special -> SetVoice of a patched copy (voice_patch.py)
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
    FM3_SPECIAL = auto()          # its $F7 on FM
    LFO = auto()                  # its $FC
    LEGATO = auto()               # Space Harrier II's $EE
    PITCH_ENVELOPE = auto()       # its $F4
    PAN_STEP = auto()             # its pan animation (a track flag): a step each read


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
class PanStep(Pan):
    """A pan animation's step: the pan a read sets (Space Harrier II's track flag bit 6).  A Pan to
    everything that reads one; the MOD writes it as 8xx where no other effect is (D2)."""
    flag = CoordFlag.PAN_STEP


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
class Lfo(PlayedEffect):
    """The hardware LFO: the chip's frequency (every channel under it takes it), and how far
    it moves this track (0, 0: not at all).  The walk sets each note's voice a copy under the
    LFO it plays with (core/smps/lfo.py)."""
    flag = CoordFlag.LFO
    frequency: int                # $22's, 0-7
    fms: int                      # B4's pitch sensitivity, 0-7
    ams: int                      # B4's level sensitivity, 0-3


@dataclass(frozen=True)
class SetPitchEnvelope(PlayedEffect):
    """The pitch envelope each note plays from its read on (PlaybackRules.pitch_envelopes,
    core/smps/pitch_envelope.py); 0: none."""
    flag = CoordFlag.PITCH_ENVELOPE
    index: int


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
class Legato(DriverEffect):
    """Every note ties to the one before, until switched off: no key-off at a read, as if each
    read followed smpsNoAttack (a rest still keys off: the note after it attacks)."""
    flag = CoordFlag.LEGATO
    mode: int                     # the driver's byte: 1 on, anything else off

    @property
    def on(self) -> bool:
        return self.mode == 1


@dataclass(frozen=True)
class VoiceRegister(DriverEffect):
    """A YM2612 operator register written over the track's voice until the next voice set."""
    flag = CoordFlag.VOICE_REGISTER
    register: int                 # as the track writes it: its channel's number in the low bits
    value: int


@dataclass(frozen=True)
class Fm3Special(DriverEffect):
    """YM2612 channel 3's special mode: each operator plays the note's frequency word plus its
    own offset, from the next note; all 0 sounds as normal mode.  Operands in a voice's operator
    order (SMPS_OP_TO_REG_OFFSET): OP4, OP3, OP2, OP1."""
    flag = CoordFlag.FM3_SPECIAL
    op4: int                      # the channel's own A2 / A6
    op3: int
    op2: int
    op1: int

    @property
    def offsets(self) -> tuple[int, ...] | None:
        """The offsets in a voice's operator order; None: all 0, normal mode."""
        offsets = (self.op4, self.op3, self.op2, self.op1)
        return offsets if any(offsets) else None


def _subclasses(kind: type) -> list[type]:
    return [sub for cls in kind.__subclasses__() for sub in (cls, *_subclasses(cls))]


_BY_FLAG: dict[CoordFlag, type[SmpsEffect]] = {
    cls.flag: cls for kind in (PlayedEffect, DriverEffect) for cls in _subclasses(kind)}
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
