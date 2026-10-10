"""Space Harrier II's voices: no records but register lists, each (register, value) pairs of
channel 0's registers up to `$83`, which the driver writes in order (Z80 $0880, adding the channel).

    voice table   a word per voice: its list's Z80 address
    a full voice  B0 and every operator register 0x30-0x8F, B4 (pan) or not; carriers take the volume
    a patch       some registers alone (voices 18, 20, 22: B4, or DT/MUL and B4): written over the
                  voice before.  Read as an empty voice; an $EF that names one is refused (variant.py).
                  No song sets one.
    $BC           written by voices 73 and 74 where B4 belongs: the chip has no such register (no pan)
"""

from __future__ import annotations

from core.chips import OPERATOR_SLOT_OFFSETS, REG_FEEDBACK_ALGORITHM, REG_PAN, OperatorReg
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.rom.voices import voice_from_registers
from core.smps import REGISTER_FIELDS, SmpsVoice

_WORD = 2
_END = 0x83
_MOST_PAIRS = 0x20                   # past a full voice's 26: no list
_NO_REGISTER = 0xBC

# What a full voice writes; what a list may write besides
_OPERATOR_REGISTERS = frozenset(OperatorReg(base) + slot for base in REGISTER_FIELDS for slot in OPERATOR_SLOT_OFFSETS)
_FULL_VOICE = _OPERATOR_REGISTERS | {REG_FEEDBACK_ALGORITHM}
_WRITTEN = _FULL_VOICE | {REG_PAN, _NO_REGISTER}


def read_sh2_voices(memory: SoundMemory, table: int, count: int) -> list[SmpsVoice]:
    """The first `count` voices of the voice table at `table`; a patch reads as an empty voice."""
    voices = []
    for index in range(count):
        registers = voice_registers(memory, voice_list(memory, table, index))
        voices.append(voice_from_registers(index, registers) if is_full_voice(registers) else SmpsVoice(index=index))
    return voices


def voice_list(memory: SoundMemory, table: int, index: int) -> int:
    """Where voice `index`'s register list is."""
    return memory.header_pointer(table, table + index * _WORD)


def voice_registers(memory: SoundMemory, at: int) -> dict[int, int]:
    """The registers the list at `at` writes, the last value of each ($BC dropped: no register)."""
    registers: dict[int, int] = {}
    for _ in range(_MOST_PAIRS):
        register = memory.byte(at)
        if register == _END:
            registers.pop(_NO_REGISTER, None)
            return registers
        if register not in _WRITTEN:
            raise RomError(f"${at:X}: a voice writes register ${register:02X}: not a voice's")
        registers[register] = memory.byte(at + 1)
        at += _WORD
    raise RomError(f"${at:X}: a voice list with no ${_END:02X} in {_MOST_PAIRS} pairs")


def is_full_voice(registers: dict[int, int]) -> bool:
    """Whether a list sets a whole voice, not a patch over the one before."""
    return registers.keys() >= _FULL_VOICE
