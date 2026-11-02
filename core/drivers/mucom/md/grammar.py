"""Streets of Rage 2's track grammar: MUCOM's language (core/rom/grammar/mucom.py) as Mucom-MD reads
it.  Its dialect and its own flags:

    PSG notes   octave o plays C(o+1): the table runs from o1 A (A2) to o7 B (B8); o1 C-G# (and o0)
                clamp to the largest divider, 12 c under A2: read as A2 (the clamp as heard)
    $F8 n       pan: ror.b #2 -> 0 none, 1 right, 2 left, 3 centre
    $F4         a half cycle is count steps, the counter first loaded with half of it ($40DE4); a
                PSG track's depth x 266 / 256 ($41F00, as $F4 sets it)
    $FF s ...   a prefix ($41B94, FM and PSG): F3 n soft release on at level n, F5 x soft release
                off (x 0) or on, F4 x a rest keys off; F0 F1 F2 read and skipped.  The drum
                track's ($4287A) skips them all
"""

from __future__ import annotations

import dataclasses

from core.rom.grammar import Instruction, as_instruction
from core.rom.grammar.mucom import SEMITONES, MucomDialect, octave_and_semitone
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.smps import FIRST_NOTE, AdsrEnvelope, ModSet, NoiseRate, PsgForm, SoftRelease

# The PSG table: from octave 1's A to octave 7's B (3 octave bits)
PSG_FIRST = SEMITONES * 1 + 9
PSG_NOTES = 8 * SEMITONES - PSG_FIRST

_PAN_B4 = (0x00, 0x40, 0x80, 0xC0)          # $F8 n, ror.b #2: B4's speaker bits
_VIBRATO_STEPS = 0                           # a half cycle: count steps
_PSG_DEPTH, _PSG_DEPTH_SHIFT = 0x10A, 8      # a PSG track's vibrato depth x 266 / 256
_ADSR_BYTES = 6
_TONES_AGAIN = 8                             # $F7 8: the track's own tone channel again
_WHITE_NOISE = 0xE4                          # the noise register: white, rate 0
_RATE_TOPS = (11, 22, 0xFF)                  # $F8 n: the last n of each rate

# $FF's sub-commands and their operand bytes (the jump table at $41BA8 / $4288E)
_PREFIX_OPERANDS = {0xF0: 1, 0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF4: 1, 0xF5: 1}
_SOFT_RELEASE_LEVEL, _REST_KEY_OFF, _SOFT_RELEASE_ON = 0xF3, 0xF4, 0xF5


def _psg_note(memory: SoundMemory, at: int) -> int:
    """A PSG note byte as its SMPS number: its index in the table from A2, a clamped note at 0."""
    octave, semitone = octave_and_semitone(memory, at)
    return FIRST_NOTE + max(0, SEMITONES * octave + semitone - PSG_FIRST)


DIALECT = MucomDialect(psg_note=_psg_note, pan_b4=_PAN_B4, vibrato_steps=_VIBRATO_STEPS)


def prefix(memory: SoundMemory, address: int) -> Instruction:
    """`$FF s ...` on an FM or PSG track: the soft release ($41BB4: F3 n on at n; $41BC2: F5 x on,
    or off at 0), a rest's key-off refused (no song sets it), F0-F2 skipped as the driver skips them."""
    sub, length = _prefix(memory, address)
    operand = memory.byte(address + 2)
    if sub == _SOFT_RELEASE_LEVEL:
        return as_instruction(SoftRelease(operand, on=True), length)
    if sub == _SOFT_RELEASE_ON:
        return as_instruction(SoftRelease(None, on=operand != 0), length)
    if sub == _REST_KEY_OFF:
        raise RomError(f"${address:X}: $FF $F4 (a rest keys off): not converted")
    return Instruction((), length, True, dropped=f"$FF ${sub:02X} (skipped)")


def drum_prefix(memory: SoundMemory, address: int) -> Instruction:
    """`$FF s ...` on the drum track: read and skipped."""
    sub, length = _prefix(memory, address)
    return Instruction((), length, True, dropped=f"$FF ${sub:02X} (skipped)")


def psg_vibrato(memory: SoundMemory, address: int) -> Instruction:
    """`$F4` on a PSG track: the depth scaled as the driver stores it ($41F00: muls #266, asr #8)."""
    read = DIALECT.vibrato(memory, address)
    mod = read.ops[0].effect if read.ops else None
    if not isinstance(mod, ModSet):
        return read
    return as_instruction(dataclasses.replace(mod, delta=mod.delta * _PSG_DEPTH >> _PSG_DEPTH_SHIFT), read.length)


def envelope(memory: SoundMemory, address: int) -> Instruction:
    """`$FA` six bytes on a PSG track: the ADSR envelope itself ($42444)."""
    return as_instruction(AdsrEnvelope(*memory.bytes_at(address + 1, _ADSR_BYTES)), 1 + _ADSR_BYTES)


def noise(memory: SoundMemory, address: int) -> Instruction:
    """`$F7 n` on a PSG track: n 8 gives the noise channel back, any other takes it, white at rate 0
    ($424E4: $E4 whatever n)."""
    if memory.byte(address + 1) == _TONES_AGAIN:
        return as_instruction(PsgForm(None), 2)
    return as_instruction(PsgForm(_WHITE_NOISE), 2)


def noise_rate(memory: SoundMemory, address: int) -> Instruction:
    """`$F8 n` on a PSG track: the noise's rate, n 0-11 rate 0, 12-22 rate 1, any more 2 ($42560)."""
    n = memory.byte(address + 1)
    rate = next(r for r, top in enumerate(_RATE_TOPS) if n <= top)
    return as_instruction(NoiseRate(rate), 2)


def drum_vibrato(memory: SoundMemory, address: int) -> Instruction:
    """`$F4` on the drum track: read as FM's, nothing for a sample to play."""
    return Instruction((), DIALECT.vibrato(memory, address).length, True, dropped="vibrato (no DAC effect)")


def _prefix(memory: SoundMemory, address: int) -> tuple[int, int]:
    """$FF's sub-command and the prefix's length."""
    sub = memory.byte(address + 1)
    operands = _PREFIX_OPERANDS.get(sub)
    if operands is None:
        raise RomError(f"${address:X}: $FF ${sub:02X} reads past the driver's prefix table")
    return sub, 2 + operands
