"""Time Trax's voices: a table of each voice's big-endian address (DriverTables.voices); the $88 n
handler ($0F2E) copies voice n's 26 bytes into the track and writes them in register order.

    B0   B4   then 4 bytes each of $30 $40 (TL) $50 $60 $70 $80, operators in register order:
              OP1 OP3 OP2 OP4 (+0 +4 +8 +C)

B4 holds AMS / FMS alone: the track's pan ($87) is OR'd in as it is written, so a voice pans
nothing.  Entry 0 points outside the bank (no song sets it): read as an empty voice.
"""

from __future__ import annotations

from dataclasses import replace

from core.chips import OperatorReg
from core.rom.memory import SoundMemory
from core.rom.variant import VoiceLayout
from core.rom.voices import read_voice
from core.smps import SmpsVoice

_WORD = 2
VOICE = VoiceLayout((OperatorReg.DT_MUL, OperatorReg.TL, OperatorReg.KS_AR, OperatorReg.AM_D1R, OperatorReg.D2R,
                     OperatorReg.D1L_RR), pan=True, operator_offsets=(0x00, 0x04, 0x08, 0x0C))


def read_voices(memory: SoundMemory, table: int, count: int) -> list[SmpsVoice]:
    """The first `count` voices of the table at `table`; one outside the bank reads empty."""
    voices = []
    for index in range(count):
        at = memory.header_pointer(table, table + index * _WORD)
        if not memory.contains(at, VOICE.size):
            voices.append(SmpsVoice(index=index))
            continue
        voices.append(replace(read_voice(memory, at, index, VOICE), pan=None))
    return voices
