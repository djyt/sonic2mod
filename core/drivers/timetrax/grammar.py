"""Time Trax's track grammar: each byte (commands.py) as the ops the SMPS walk knows, read through
the song's walk (TimeTraxMemory.reading: a note's fixed duration, a loop end's body, a wait's
frames).

    $00 d / $01-$7F d   a rest / note n: NOTE ($80 + n: the FM table's entry n, $81 C0), DURATION d
    $80 $81 $84 $85     STOP, JUMP, CALL, RETURN (big-endian addresses in the bank)
    $82 n ... $83       LOOP: n passes of the body after `$82 n`
    $86 n               nothing: the walk gives the notes after it their duration
    $87 n               Pan: B4's speakers by the driver's table (n $FF left, 0 centre, 1 right)
    $88 n               SetVoice
    $89 n               SetTransposition n (signed): the note's table entry n + it
    $8A v               ScaledVolume v: the masked operators' output x v / 256 (volume.py)
    $8B / $8C           Legato off / on (a note neither keys off nor on)
    $8D n / $8E         the sync: the signal nothing, the wait a rest of the frames it waits
    $96 n / $97         Gate n + 1 (and KeyOffAfter cleared) / Gate 0: each frame after the countdown
    $98 n / $99         KeyOffAfter n - 1 (and Gate cleared) / KeyOffAfter 0   ($1293: n against it)
    $A8 d / $A9         Detune d (signed, added to the frequency word) / 0
    $AA m / $AB         VolumeMask m (bit 3: TL $40 ... bit 0: $4C) / None: the carriers

A rest writes nothing: the note sounds on to a read's key-off (TrackRules.key_off.rest_holds).
Read and dropped for now, each named (docs/todo/time_trax.md Phase 2): glide, portamento, vibrato,
volume envelopes ($8A stops one: from 2.4).  Refused (no song plays
them): the signalled jumps, the random jump, the portamento's transposition mode, release
envelopes, the LFO, the DAC.
"""

from __future__ import annotations

from core.rom.flags import FlagSpec, read
from core.rom.grammar import Instruction, as_instruction, flag_instruction, track_label
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.rom.variant import SmpsVariant
from core.smps import (
    REST,
    ChannelType,
    Detune,
    Gate,
    KeyOffAfter,
    Legato,
    Op,
    OpKind,
    Pan,
    ScaledVolume,
    SetTransposition,
    SetVoice,
    SmpsEffect,
    VolumeMask,
    signed_byte,
)

from .commands import (
    COMMANDS,
    DETUNE,
    DETUNE_OFF,
    FIRST_COMMAND,
    GATE,
    GATE_OFF,
    KEY_OFF_AFTER,
    KEY_OFF_AFTER_OFF,
    LEGATO,
    MASK,
    MASK_CARRIERS,
    PAN,
    RE_KEY,
    REST_BYTE,
    TRANSPOSE,
    VOICE,
    VOLUME,
    Steer,
    counted,
)
from .memory import TimeTraxMemory

_NOTE_BASE = REST                       # note n plays the FM table's entry n: SMPS's $80 + n
_LEGATO_ON = 1                          # Legato's mode byte
_PAN_INDEX = 1                          # $87 n: the table's entry n + 1 ($FF wraps to 0)
_BYTE = 0xFF
_CHECK_LAG = 1                          # $1293 checks after the countdown: n + 1 frames before the end,
                                        # n - 1 after the start (the read's frame unchecked)

def instruction(memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType) -> Instruction:
    """The instruction at `address` (SmpsVariant.grammar): a note or rest, else a command (FLAGS)."""
    if not isinstance(memory, TimeTraxMemory):
        raise TypeError("Time Trax's track code reads through its song's walk (TimeTraxMemory)")
    if kind is not ChannelType.FM:
        raise RomError(f"${address:X}: a {kind} track: Time Trax's music plays FM tracks alone")
    byte = memory.byte(address)
    if byte < FIRST_COMMAND:
        return _note(memory, address, byte)
    return flag_instruction(memory, address, variant, kind)


def command(memory: SoundMemory, address: int) -> Instruction:
    """The command at `address`, as its bytes and the song's walk read it."""
    assert isinstance(memory, TimeTraxMemory)
    byte = memory.byte(address)
    spec = COMMANDS[byte]
    length = 1 + spec.operands
    operand = memory.byte(address + 1) if spec.operands else 0

    match spec.steer:
        case Steer.STOP:
            return Instruction((Op(OpKind.STOP),), length, False)
        case Steer.JUMP | Steer.CALL:
            target = memory.code_pointer(address + 1)
            kind = OpKind.JUMP if spec.steer is Steer.JUMP else OpKind.CALL
            return Instruction((Op(kind, name=track_label(target)),), length, spec.steer is Steer.CALL, target)
        case Steer.RETURN:
            return Instruction((Op(OpKind.RETURN),), length, False)
        case Steer.LOOP:
            return Instruction((), length, True, target=address + length)
        case Steer.LOOP_END:
            body, passes = _read(memory.reading.loop_ends, address)
            return Instruction((Op(OpKind.LOOP, value=passes, name=track_label(body)),), length, True, target=body)
        case Steer.FIXED:
            return Instruction((), length, True)
        case Steer.SIGNAL:
            return Instruction((), length, True, dropped="signal (its wait read as a rest)")
        case Steer.WAIT:
            frames = _read(memory.reading.waits, address)
            return Instruction(_rest(frames) if frames else (), length, True)
        case Steer.REFUSED:
            raise RomError(f"${address:X}: ${byte:02X} ({spec.what}): no song plays it, not read")

    if spec.later:
        return Instruction((), length, True, dropped=f"{spec.what} (not converted yet)")
    return as_instruction(_effect(memory, address, byte, operand), length)


# Every command, read by `command` (the FM tracks' table, $0DC8)
FLAGS: dict[int, FlagSpec] = {byte: read(command) for byte in COMMANDS}


def _note(memory: TimeTraxMemory, address: int, byte: int) -> Instruction:
    """A note or a rest and its duration: the fixed one, or its byte."""
    fixed = _read(memory.reading.fixed, address)
    duration = counted(fixed or memory.byte(address + 1))
    length = 1 if fixed else 2
    if byte == REST_BYTE:
        return Instruction(_rest(duration), length, True)
    if byte >= len(memory.tables.fm_words):
        raise RomError(f"${address:X}: note ${byte:02X} reads past the FM table")
    return Instruction((Op(OpKind.NOTE, value=_NOTE_BASE + byte), Op(OpKind.DURATION, value=duration)), length, True)


def _rest(frames: int) -> tuple[Op, ...]:
    return Op(OpKind.NOTE, value=REST), Op(OpKind.DURATION, value=frames)


def _effect(memory: TimeTraxMemory, address: int, byte: int, operand: int) -> SmpsEffect | tuple[SmpsEffect, ...]:
    """A command the song keeps as an effect, or the walk resolves (a transposition, the key-offs)."""
    if byte == PAN:
        pans = memory.tables.pans
        index = (operand + _PAN_INDEX) & _BYTE
        if index >= len(pans):
            raise RomError(f"${address:X}: pan ${operand:02X} reads past the driver's pan table")
        return Pan(pans[index])
    if byte == VOICE:
        return SetVoice(operand)
    if byte in (RE_KEY, LEGATO):
        return Legato(_LEGATO_ON if byte == LEGATO else 0)
    if byte in (DETUNE, DETUNE_OFF):
        return Detune(signed_byte(operand))           # $A9: no operand, 0
    if byte == TRANSPOSE:
        return SetTransposition(signed_byte(operand))
    if byte == GATE:
        return Gate(operand + _CHECK_LAG if operand else 0), KeyOffAfter(0)
    if byte == KEY_OFF_AFTER:
        return KeyOffAfter(max(operand - _CHECK_LAG, 0)), Gate(0)
    if byte == GATE_OFF:
        return Gate(0)
    if byte == KEY_OFF_AFTER_OFF:
        return KeyOffAfter(0)
    if byte == VOLUME:
        return ScaledVolume(operand)
    if byte in (MASK, MASK_CARRIERS):
        return VolumeMask(operand if byte == MASK else None)
    raise AssertionError(f"${byte:02X}: a command with no reading")


def _read(found: dict, address: int):
    """What the song's walk found at `address`."""
    if address not in found:
        raise RomError(f"${address:X}: code the song's walk never reached")
    return found[address]
