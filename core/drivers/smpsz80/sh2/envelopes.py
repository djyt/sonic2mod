"""Space Harrier II's pitch envelopes (Z80 $037A): a word per envelope, then each one's bytes, read
into core/smps/pitch_envelope.py's steps.

    $00-$7F   an offset added to the note's word          $80     restart
    $86-$FF   a negative one (the byte, signed)            $81-$83 hold: the word stays
    $84 n     the scale grows by n                         $85 n   jump to byte n

An envelope's index is SetPitchEnvelope's ($F4, a track record's byte 6): 1 is the table's first.
The table runs up to the first envelope's bytes.

    Space Harrier II   table $863B: 4 envelopes.  1-3 scoop into the note, then a vibrato that
                       deepens each pass; 4 a fall
"""

from __future__ import annotations

from core.rom.image import RomError
from core.smps import PitchEnvelope
from core.smps.pitch_envelope import Hold, Jump, Restart, Scale, Step

from .memory import Sh2Memory

_WORD = 2
_RESTART = 0x80
_HOLDS = range(0x81, 0x84)
_SCALE, _JUMP = 0x84, 0x85
_FIRST_NEGATIVE = 0x86
_BYTE = 0x100
_MOST_BYTES = 0x40             # past this without a restart, hold or jump: no envelope


def read_pitch_envelopes(memory: Sh2Memory) -> dict[int, PitchEnvelope]:
    """Every envelope of the table, by index from 1."""
    table = memory.tables.pitch_envelopes
    first = memory.header_pointer(table, table)
    count = (first - table) // _WORD
    if not 0 < count <= _MOST_BYTES:
        raise RomError(f"pitch envelope table ${table:X}: its first envelope at ${first:X}")
    return {i + 1: _envelope(memory, memory.header_pointer(table, table + i * _WORD)) for i in range(count)}


def _envelope(memory: Sh2Memory, start: int) -> PitchEnvelope:
    """The envelope's bytes from `start`, to the restart, hold or jump that ends them."""
    steps: list[Step] = []
    step_at: dict[int, int] = {}         # each step's byte position
    at = 0
    while at < _MOST_BYTES:
        step_at[at] = len(steps)
        byte = memory.byte(start + at)
        if byte == _RESTART:
            steps.append(Restart())
        elif byte in _HOLDS:
            steps.append(Hold())
        elif byte == _JUMP:
            target = memory.byte(start + at + 1)
            if target not in step_at:
                raise RomError(f"${start + at:X}: a pitch envelope's jump to byte {target}, no step's start")
            steps.append(Jump(step_at[target]))
        elif byte == _SCALE:
            steps.append(Scale(memory.byte(start + at + 1)))
            at += 2
            continue
        else:
            steps.append(byte - _BYTE if byte >= _FIRST_NEGATIVE else byte)
            at += 1
            continue
        return PitchEnvelope(tuple(steps))
    raise RomError(f"${start:X}: a pitch envelope with no end in {_MOST_BYTES} bytes")
