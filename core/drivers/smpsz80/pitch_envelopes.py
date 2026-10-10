"""An SMPS Z80 driver's frequency envelopes: a word per envelope, then each one's bytes, read into
core/smps/pitch_envelope.py's steps by the driver's own command bytes (EnvelopeBytes).  The table
runs up to the first envelope's bytes; an envelope's index is SetPitchEnvelope's ($F4): 1 is the
table's first.

                      offset   negative   restart   hold        scale n   jump n   the scale kept
    Space Harrier II  $00-$7F  $86-$FF    $80       $81-$83     $84       $85      in a byte
    OutRun            $00-$7F  $85-$FF    $80       $81 $83     $84       $82      whole (a word add)
"""

from __future__ import annotations

from dataclasses import dataclass

from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.smps import PitchEnvelope
from core.smps.pitch_envelope import Hold, Jump, Restart, Scale, Step

_WORD = 2
_BYTE = 0x100
_MOST_BYTES = 0x40             # past this without a restart, hold or jump: no envelope


@dataclass(frozen=True)
class EnvelopeBytes:
    """A driver's envelope command bytes; below $80 and from `first_negative`, a byte is an offset."""

    restart: int
    holds: frozenset[int]
    scale: int
    jump: int
    first_negative: int
    byte_scale: bool               # the scaled offset kept in a byte (PitchEnvelope.byte_scale)


def read_envelope_table(memory: SoundMemory, table: int, commands: EnvelopeBytes) -> dict[int, PitchEnvelope]:
    """Every envelope of the table at `table`, by index from 1."""
    first = memory.header_pointer(table, table)
    count = (first - table) // _WORD
    if not 0 < count <= _MOST_BYTES:
        raise RomError(f"pitch envelope table ${table:X}: its first envelope at ${first:X}")
    return {i + 1: _envelope(memory, memory.header_pointer(table, table + i * _WORD), commands) for i in range(count)}


def _envelope(memory: SoundMemory, start: int, commands: EnvelopeBytes) -> PitchEnvelope:
    """The envelope's bytes from `start`, to the restart, hold or jump that ends them."""
    steps: list[Step] = []
    step_at: dict[int, int] = {}         # each step's byte position
    at = 0
    while at < _MOST_BYTES:
        step_at[at] = len(steps)
        byte = memory.byte(start + at)

        # Offsets, and the scale: on to the next byte
        if byte == commands.scale:
            steps.append(Scale(memory.byte(start + at + 1)))
            at += 2
            continue
        if byte < commands.restart or byte >= commands.first_negative:
            steps.append(byte - _BYTE if byte >= commands.first_negative else byte)
            at += 1
            continue

        # The commands that end the bytes
        if byte == commands.restart:
            steps.append(Restart())
        elif byte in commands.holds:
            steps.append(Hold())
        elif byte == commands.jump:
            target = memory.byte(start + at + 1)
            if target not in step_at:
                raise RomError(f"${start + at:X}: a pitch envelope's jump to byte {target}, no step's start")
            steps.append(Jump(step_at[target]))
        else:
            raise RomError(f"${start + at:X}: ${byte:02X} is no pitch envelope command")
        return PitchEnvelope(tuple(steps), commands.byte_scale)
    raise RomError(f"${start:X}: a pitch envelope with no end in {_MOST_BYTES} bytes")
