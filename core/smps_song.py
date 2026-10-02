"""The parsed song (SmpsParser's intermediate representation) and what its effects say."""

from dataclasses import dataclass, field

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


@dataclass
class SmpsEffect:
    effect_type: str      # e.g. "smpsSetvoice", "smpsModSet", etc.
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
    has_jump: bool = False
    jump_target_label: str = ""
    # label -> index into `events` of the first event AFTER that label in this channel's stream.
    # A tick alone cannot say whether a zero-duration event at the label's tick (a coordination
    # flag written just before the label) is inside the loop that jumps to it.
    label_event_index: dict = field(default_factory=dict)


# A YM2612 channel's operator count: every smpsVc* macro but the algorithm one states four bytes
_OPERATORS = 4


@dataclass
class SmpsVoice:
    index: int
    algorithm: int = 0
    feedback: int = 0
    # Store raw params for informational purposes
    params: dict = field(default_factory=dict)

    def operator_values(self, macro: str) -> list[int]:
        """One smpsVc* macro's operator bytes as written: '$00, $05, $00, $05' → [0, 5, 0, 5].
        A macro the voice leaves out, or a value it leaves out, reads as 0."""
        raw = self.params.get(macro)
        if not raw:
            return [0] * _OPERATORS
        vals = [int(v.strip().lstrip('$'), 16) for v in raw.split(',')]
        return (vals + [0] * _OPERATORS)[:_OPERATORS]


@dataclass
class SmpsSong:
    header: SmpsSongHeader
    channels: list = field(default_factory=list)  # list of SmpsChannel
    voices: list = field(default_factory=list)     # list of SmpsVoice
    label_tick_pos: dict = field(default_factory=dict)  # label_name -> cumulative tick position

    def end_tick(self) -> int:
        """The tick the last event of any channel ends at (a note's duration included)."""
        return max((ev.tick_position + (ev.note.duration if ev.note else 0)
                    for ch in self.channels for ev in ch.events), default=0)

    def loop_target_tick(self) -> int | None:
        """The tick the song loops back to: the latest smpsJump target over the channels;
        None when no channel jumps."""
        ticks = [self.label_tick_pos.get(ch.jump_target_label) for ch in self.channels
                 if ch.has_jump and ch.jump_target_label]
        return max((t for t in ticks if t is not None), default=None)


# --- effect parameters ---


def pan_side(params: list) -> str:
    """The speaker an smpsPan sends the channel to: "L", "R", or "C" for both (params arrive
    as one 'panLeft, $00' string)."""
    direction = str(params[0]).split(',')[0].strip().lower() if params else ''
    return {'panleft': "L", 'panright': "R"}.get(direction, "C")


def pan_is_hard(params: list) -> bool:
    """True for smpsPan panLeft / panRight."""
    return pan_side(params) != "C"
