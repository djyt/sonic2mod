"""Space Harrier II's pitch envelopes (Z80 $037A), read by the family's reader (../pitch_envelopes.py)
with its command bytes.

    Space Harrier II   table $863B: 4 envelopes.  1-3 scoop into the note, then a vibrato that
                       deepens each pass; 4 a fall
"""

from __future__ import annotations

from core.smps import PitchEnvelope

from ..pitch_envelopes import EnvelopeBytes, read_envelope_table
from .memory import Sh2Memory

_COMMANDS = EnvelopeBytes(restart=0x80, holds=frozenset(range(0x81, 0x84)), scale=0x84, jump=0x85,
                          first_negative=0x86, byte_scale=True)


def read_pitch_envelopes(memory: Sh2Memory) -> dict[int, PitchEnvelope]:
    """Every envelope of the table, by index from 1."""
    return read_envelope_table(memory, memory.tables.pitch_envelopes, _COMMANDS)
