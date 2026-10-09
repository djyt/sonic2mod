"""The parsed song (SmpsParser's intermediate representation) and what its effects say."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ..chips import CARRIER_OFFSETS_BY_ALG, TL_MASK, OperatorReg
from .driver_tables import SMPS_OP_TO_REG_OFFSET
from .effects import SetTempoMod, SmpsEffect
from .rules import PlaybackRules
from .tempo import NO_TEMPO_HOLDS, TempoSegment, tempo_schedule

if TYPE_CHECKING:
    from .percussion import FmDrum  # it imports SmpsVoice from here

# ---------------------------------------------------------------------------
# Intermediate representation data classes
# ---------------------------------------------------------------------------

REST = 0x80           # nRst: the note byte that rests


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
    # Shaped by the driver's key-on run-out (core/smps/run_out.py): a note cut short, or the rest the
    # cut leaves.  Off the song's own rhythm: the row grid (core.plan.derive) leaves it out
    run_out: bool = False


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


class ChannelType(StrEnum):
    """A track's chip, as the song's header says (never its name: Type 0 FM's drum track is FM3)."""

    DAC = "DAC"
    FM = "FM"
    PSG = "PSG"


@dataclass
class SmpsChannelHeader:
    channel_type: ChannelType
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
    # The chip channel a music track plays on ("FM3"), where its driver's order is not header
    # order (Sonic 1's: DAC, FM1.., PSG1..); "" = header order.  core/smps/names.py source_names
    chip_channel: str = ""


@dataclass
class SmpsSongHeader:
    voice_label: str = ""
    fm_count: int = 0
    psg_count: int = 0
    tempo_divider: int = 1
    tempo_modifier: int = 5
    channels: list[SmpsChannelHeader] = field(default_factory=list)
    # True when parsed from smpsHeader*SFX* macros.  SFX have no tempo modifier byte and run
    # one tick per V-int unconditionally — the music (modifier-1)/modifier rate correction
    # must not be applied to them.
    is_sfx: bool = False


@dataclass
class SmpsChannel:
    header: SmpsChannelHeader
    events: list[SmpsEvent] = field(default_factory=list)
    rules: PlaybackRules = field(kw_only=True)  # its driver's: the song's
    has_jump: bool = False        # the channel ends in a jump back: a loop
    loop_tick: int | None = None  # the tick the jump returns to
    # Index into `events` of the loop's first event.  A tick alone cannot say whether a
    # zero-duration event at the loop's tick (a coordination flag written just before the
    # target) is inside the loop.  None: the loop is taken from loop_tick on.
    loop_event_index: int | None = None
    loop_label: str = ""          # the assembly's name for the target, for display; a lift has none


# A YM2612 channel's operator count
_OPERATORS = 4
_BYTE = 0xFF


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
    # Register B4 (L R AMS FMS) where the driver stores it in the voice: setting the voice pans
    # the track (the walk writes a PAN after its smpsSetvoice).  None: Sonic 1's, pan by flag only
    pan: int | None = None

    def operator_values(self, field_: VoiceField) -> list[int]:
        """One field's four operator values; a field the voice leaves out, or a value it leaves
        out, reads as 0."""
        vals = list(self.operators.get(field_, ()))
        return (vals + [0] * _OPERATORS)[:_OPERATORS]

    @property
    def feedback_algorithm(self) -> int:
        """Register B0."""
        return (self.feedback & 0x7) << 3 | (self.algorithm & 0x7)

    @property
    def carrier_registers(self) -> tuple[int, ...]:
        """The carriers' TL registers (channel 0), in the driver's FMInstrumentTLTable order."""
        return tuple(OperatorReg.TL + off for off in CARRIER_OFFSETS_BY_ALG[self.algorithm & 0x7])

    def registers(self, tl_offset: int = 0) -> dict[int, int]:
        """The operator registers 0x30-0x9F of channel 0 as the driver writes the voice, operator
        by operator: the track volume `tl_offset` added to the carriers' TL (add.b: modulo 256;
        the chip reads 7 bits), SSG-EG off."""
        f = {name: self.operator_values(name) for name in VoiceField}
        carriers = self.carrier_registers
        regs: dict[int, int] = {}
        for op, off in enumerate(SMPS_OP_TO_REG_OFFSET):
            tl = f[VoiceField.TOTAL_LEVEL][op] & _BYTE
            if OperatorReg.TL + off in carriers:
                tl = (tl + tl_offset) & _BYTE
            regs[OperatorReg.DT_MUL + off] = (f[VoiceField.DETUNE][op] & 0x7) << 4 | f[VoiceField.MULTIPLE][op] & 0xF
            regs[OperatorReg.TL + off] = tl
            regs[OperatorReg.KS_AR + off] = (f[VoiceField.RATE_SCALE][op] & 0x3) << 6 | f[VoiceField.ATTACK_RATE][op] & 0x1F
            regs[OperatorReg.AM_D1R + off] = (f[VoiceField.AMP_MOD][op] & 0x1) << 7 | f[VoiceField.DECAY_RATE_1][op] & 0x1F
            regs[OperatorReg.D2R + off] = f[VoiceField.DECAY_RATE_2][op] & 0x1F
            regs[OperatorReg.D1L_RR + off] = (f[VoiceField.DECAY_LEVEL][op] & 0xF) << 4 | f[VoiceField.RELEASE_RATE][op] & 0xF
            regs[OperatorReg.SSG_EG + off] = 0
        return regs


    def chip_registers(self, tl_offset: int = 0) -> dict[int, int]:
        """registers() as the chip reads them: TL is 7 bits (SMPS2ASM sets bit 7 on the carriers,
        and some asm writes a modulator's TL as $80)."""
        tl = range(OperatorReg.TL, OperatorReg.KS_AR)
        return {r: v & TL_MASK if r in tl else v for r, v in self.registers(tl_offset).items()}


@dataclass
class SmpsSong:
    header: SmpsSongHeader
    channels: list[SmpsChannel] = field(default_factory=list)
    voices: list[SmpsVoice] = field(default_factory=list)
    # The drum track's FM drum programs by DAC name (Type 0 FM's drum81 ...; core/smps/percussion.py);
    # empty where the drum track plays DAC samples
    fm_drums: dict[str, FmDrum] = field(default_factory=dict)
    # Flags read and left out (a ROM's driver: pan animation, queued sounds), by name
    dropped: dict[str, int] = field(default_factory=dict)
    # What its driver plays it by: tables, envelopes, drum names, timing (each channel holds the same)
    rules: PlaybackRules = field(kw_only=True)

    def end_tick(self) -> int:
        """The tick the last event of any channel ends at (a note's duration included)."""
        return max((ev.tick_position + (ev.note.duration if ev.note else 0)
                    for ch in self.channels for ev in ch.events), default=0)

    def tempo_changes(self) -> list[tuple[int, int]]:
        """(tick, modifier) of every smpsSetTempoMod, in tick order."""
        return sorted({(ev.tick_position, ev.effect.modifier) for ch in self.channels for ev in ch.events
                       if isinstance(ev.effect, SetTempoMod)})

    def tempo_schedule(self) -> tuple[TempoSegment, ...]:
        """When the driver reads each tick (core/smps/tempo.py): the header's tempo at the driver's
        phase, then each smpsSetTempoMod.  An SFX never holds."""
        modifier = self.header.tempo_modifier
        holds = modifier > 1 and not self.header.is_sfx
        return tempo_schedule(modifier if holds else NO_TEMPO_HOLDS, self.tempo_changes(), self.rules.tempo_phase)

    def loop_target_tick(self) -> int | None:
        """The tick the song loops back to: the latest smpsJump target over the channels;
        None when no channel jumps."""
        return max((ch.loop_tick for ch in self.channels if ch.has_jump and ch.loop_tick is not None),
                   default=None)


