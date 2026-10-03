"""The parsed song (SmpsParser's intermediate representation) and what its effects say."""

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

# ---------------------------------------------------------------------------
# Intermediate representation data classes
# ---------------------------------------------------------------------------

@dataclass
class SmpsNote:
    note_value: int       # SMPS byte value (0x80=rest, 0x81=C0, etc.)
    duration: int         # Duration in ticks
    is_rest: bool = False
    is_dac: bool = False
    dac_name: str = ""    # Original DAC name (e.g. "dKick")
    is_no_attack: bool = False  # smpsNoAttack flag
    # True when synthesised from a standalone duration byte rather than an explicit note
    # byte.  The driver's .gotduration path skips FMSetFreq/PSGSetFreq entirely, so the
    # channel re-keys at its EXISTING frequency — which differs from re-deriving it if a
    # smpsChangeTransposition landed in between (SndA8 - SS Goal does exactly that).
    is_retrigger: bool = False


class CoordFlag(IntEnum):
    """The driver's coordination flags a song's events carry, by their byte (s1.sounddriver.asm
    coordflagLookup; docs/smps_driver.md).  A parse and a VGM lift both produce these: the
    SMPS2ASM macro names are core/smps/names.py's, for reading and printing assembly."""

    PAN = 0xE0                    # params: [the YM2612 B4 byte: L R AMS FMS]
    DETUNE = 0xE1                 # [FNUM offset, signed]
    NOP = 0xE2                    # [byte]
    CHAN_TEMPO_DIV = 0xE5         # [divider]
    ALTER_VOL = 0xE6              # [delta, signed]; $EC on a PSG channel
    NOTE_FILL = 0xE8              # [frames]
    CHANGE_TRANSPOSITION = 0xE9   # [semitones, signed]
    SET_TEMPO_MOD = 0xEA          # [modifier]
    SET_TEMPO_DIV = 0xEB          # [divider]
    SET_VOICE = 0xEF              # [voice index]
    MOD_SET = 0xF0                # [wait, speed, delta, steps]
    MOD_ON = 0xF1
    PSG_FORM = 0xF3               # [noise register byte]
    MOD_OFF = 0xF4
    PSG_VOICE = 0xF5              # [envelope name, fTone_01 ... fTone_09: the driver's table]


@dataclass
class SmpsEffect:
    flag: CoordFlag
    params: list = field(default_factory=list)


@dataclass
class SmpsEvent:
    """Union of note or effect event."""
    note: SmpsNote | None = None
    effect: SmpsEffect | None = None
    tick_position: int = 0  # Cumulative tick position in the channel

    @property
    def is_note(self):
        return self.note is not None

    @property
    def is_effect(self):
        return self.effect is not None


@dataclass
class SmpsChannelHeader:
    channel_type: str     # "DAC", "FM", "PSG"
    label: str
    pitch_offset: int = 0
    volume: int = 0
    # PSG-specific
    mod_byte: int = 0
    voice: int = 0
    psg_voice_label: str = ""  # initial smpsPSGvoice label from smpsHeaderPSG (e.g. "fTone_06")
    # SFX-specific: raw chanid byte from smpsHeaderSFXChannel (cFM5 = $05, cPSG3 = $C0, ...).
    # Music headers imply the hardware channel by declaration order; SFX headers do not, so
    # cFM3/cFM4/cFM5 are indistinguishable without this.  0 = not an SFX channel.
    hw_channel: int = 0


@dataclass
class SmpsSongHeader:
    voice_label: str = ""
    fm_count: int = 0
    psg_count: int = 0
    tempo_divider: int = 1
    tempo_modifier: int = 5
    channels: list = field(default_factory=list)  # list of SmpsChannelHeader
    # True when parsed from smpsHeader*SFX* macros.  SFX have no tempo modifier byte and run
    # one tick per V-int unconditionally — the music (modifier-1)/modifier rate correction
    # must not be applied to them.
    is_sfx: bool = False


@dataclass
class SmpsChannel:
    header: SmpsChannelHeader
    events: list = field(default_factory=list)  # list of SmpsEvent
    has_jump: bool = False        # the channel ends in a jump back: a loop
    loop_tick: int | None = None  # the tick the jump returns to
    # Index into `events` of the loop's first event.  A tick alone cannot say whether a
    # zero-duration event at the loop's tick (a coordination flag written just before the
    # target) is inside the loop.  None: the loop is taken from loop_tick on.
    loop_event_index: int | None = None
    loop_label: str = ""          # the assembly's name for the target, for display; a lift has none


# A YM2612 channel's operator count
_OPERATORS = 4


class VoiceField(StrEnum):
    """A YM2612 operator field a voice sets on its four operators (registers 0x30-0x9F).
    The SMPS2ASM macro spelling of each is core/smps/names.py's."""

    DETUNE = "dt"
    MULTIPLE = "mul"
    RATE_SCALE = "ks"
    ATTACK_RATE = "ar"
    AMP_MOD = "am"
    DECAY_RATE_1 = "d1r"
    DECAY_RATE_2 = "d2r"
    DECAY_LEVEL = "d1l"
    RELEASE_RATE = "rr"
    TOTAL_LEVEL = "tl"


@dataclass
class SmpsVoice:
    index: int
    algorithm: int = 0
    feedback: int = 0
    # Each field's four values in the order the driver stores the operators (SMPS_OP_TO_REG_OFFSET
    # maps each to its register slot): what the parser reads, and what a lift builds from registers
    operators: dict[VoiceField, tuple[int, ...]] = field(default_factory=dict)

    def operator_values(self, field_: VoiceField) -> list[int]:
        """One field's four operator values; a field the voice leaves out, or a value it leaves
        out, reads as 0."""
        vals = list(self.operators.get(field_, ()))
        return (vals + [0] * _OPERATORS)[:_OPERATORS]


@dataclass
class SmpsSong:
    header: SmpsSongHeader
    channels: list = field(default_factory=list)  # list of SmpsChannel
    voices: list = field(default_factory=list)     # list of SmpsVoice

    def end_tick(self) -> int:
        """The tick the last event of any channel ends at (a note's duration included)."""
        return max((ev.tick_position + (ev.note.duration if ev.note else 0)
                    for ch in self.channels for ev in ch.events), default=0)

    def loop_target_tick(self) -> int | None:
        """The tick the song loops back to: the latest smpsJump target over the channels;
        None when no channel jumps."""
        return max((ch.loop_tick for ch in self.channels if ch.has_jump and ch.loop_tick is not None),
                   default=None)


# --- effect parameters ---


_PAN_SPEAKERS = 0xC0              # B4 bits 7 (left) and 6 (right)
_PAN_LEFT = 0x80
_PAN_RIGHT = 0x40


def pan_side(params: list) -> str:
    """The speaker a PAN flag's B4 byte sends the channel to: "L", "R", or "C" for both (or
    neither, which the driver never writes for music)."""
    speakers = params[0] & _PAN_SPEAKERS if params else _PAN_SPEAKERS
    return {_PAN_LEFT: "L", _PAN_RIGHT: "R"}.get(speakers, "C")


def pan_is_hard(params: list) -> bool:
    """True for a channel panned hard left or right."""
    return pan_side(params) != "C"
