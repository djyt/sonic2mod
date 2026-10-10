"""Space Harrier II's drums: the drum track's byte is a bitmask; one read starts up to two FM drums
on FM3 in special mode and a noise drum on the PSG (Z80 $0CC0 and $0DFE, reading the same bytes).
Each drum byte is played here frame by frame, as the driver plays it, into an FmDrum.

    FM      two units, each an operator pair of FM3 (algorithm 4: two pairs, two drums):
              bit 3 (else bit 0)   unit A, OP1 -> OP2, a record (bit 5: the next record)
              bit 2                unit B, OP3 -> OP4, record $0BF0 (D6: the code means $0BE6
                                   without bit 5, no song sets it, the game plays $0BF0)
            a record, 10 bytes: flags (bit 0: unit B's operators), its voice's register list,
            op X's word (high, low), op Y's (high, low), X's step, Y's step, frames
            each frame: X's word, then its low byte + X's step; Y's word, then Y's HIGH byte =
            its low byte + Y's step (D7: the driver's, $0DA5; OP4 jumps a block on frame 2);
            the frames run out: the pair keyed off
    PSG     bit 3, else 1, else 0 or 2 (else none): tone 3's divider, its first attenuation, the
            noise register (white, clocked by tone 3), an envelope and a volume; each frame the
            envelope's step + the volume (15 at most) on the noise, + 2 on tone 3 (bit 3: tone 3
            stays silent); the envelope's $83 silences both
    voice   what every drum list leaves in the chip, then the hit's own lists (unit A's first):
            a list leaves registers out (unit A's $0C22 writes $84, OP3's, where $88 is OP2's, so
            OP2 keeps $0C58's $4F, as every rip shows)

The tables are found by the code that reads them (no game's addresses).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from core.chips import CH3_FREQ_REGS
from core.rom.image import RomError, RomImage
from core.rom.voices import voice_from_registers
from core.smps import FmDrum, FmFrame, PsgDrumFrame

from ..memory import Z80RamMemory
from ..program import driver_ram
from .locate import sh2_memory
from .memory import Sh2Memory
from .voices import voice_registers

_WORD = 2
_FIRST_DRUM, _LAST_DRUM = 0x81, 0xDF
_BIT5 = 0x20

# The drum read's dispatch ($0CF4): bit 3 -> unit A's first handler, else bit 0 -> its second;
# then bit 2 -> unit B's handler
_DISPATCH = re.compile(rb"\xCB\x5F\x28\x05\xCD(..)\x18\x05\xCB\x47\xC4(..)\xDD\x7E\x11\xCB\x57\xC4(..)", re.DOTALL)
_BIT3, _BIT0, _BIT2 = 0x08, 0x01, 0x04
# A unit A handler: ld de,unit / ld hl,record / bit 5,a / jr z / ld hl,bit 5's record
_HANDLER_A = re.compile(rb"\x11(..)\x21(..)\xCB\x6F\x28.\x21(..)", re.DOTALL)
# Unit B's: ld de,unit / ld hl,record / bit 5,a / ld hl,record / jr nz,+0 - the second, always
_HANDLER_B = re.compile(rb"\x11(..)\x21(..)\xCB\x6F\x21(..)\x20\x00", re.DOTALL)
# The frame update ($0D61): ld iy,unit / ld hl,registers / call ... / ld hl,registers / ld iy,unit
_UPDATE = re.compile(rb"\xFD\x21(..)\x21(..)\xCD..\x21(..)\xFD\x21(..)", re.DOTALL)

_RECORD = 10
_VOICE, _X_HIGH, _X_LOW, _Y_HIGH, _Y_LOW, _X_STEP, _Y_STEP, _FRAMES = 1, 3, 4, 5, 6, 7, 8, 9
_SLOT_OF_LOW_REGISTER = {low: slot for slot, low in CH3_FREQ_REGS.items()}
_KEY_OF_SLOT = {0x00: 0b0001, 0x08: 0b0010, 0x04: 0b0100, 0x0C: 0b1000}    # OP1 OP2 OP3 OP4: $28's bits 4-7
_SLOT_INDEX = {0x00: 0, 0x04: 1, 0x08: 2, 0x0C: 3}     # FmFrame.slots' order (OPERATOR_SLOT_OFFSETS)
_OWN_SLOT = 0x0C                                        # OP4: the channel's own word

# The PSG part's dispatch ($0E61): push de / bit 3 / jr nz / bit 1 / jr nz / bit 0 / jr nz / bit 2 / jr nz
_PSG_DISPATCH = re.compile(rb"\xD5\xCB\x5F\x20(.)\xCB\x4F\x20(.)\xCB\x47\x20(.)\xCB\x57\x20(.)", re.DOTALL)
# bits 3 and 1: ld hl,divider / ld de,step+tone byte / ex af / ld a,envelope / ld b,volume / ld c,noise
_PSG_TONE = re.compile(rb"\x21(..)\x11(..)\x08\x3E(.)\x06(.)\x0E(.)", re.DOTALL)
# bits 0 and 2: ld hl / ld de / ld c / (bit 2's same again) / ex af / ld a,envelope / ld b,volume
_PSG_NOISE = re.compile(rb"\x21(..)\x11(..)\x0E(.)\xCB\x57\x28\x08.{8}\x08\x3E(.)\x06(.)", re.DOTALL)
_PSG_BITS = (0x08, 0x02, 0x01)    # in the dispatch's order; bit 2 plays bit 0's
_ENVELOPES = re.compile(rb"\x0D\xFA..\x21(..)\xCD", re.DOTALL)   # dec c / jp m / ld hl,envelopes / call

_SILENT = 15
_TONE_ABOVE_NOISE = 2             # tone 3 sounds this many steps quieter than the noise
_MUTE = 0x83
_COMMAND = 0x80
_MOST_STEPS = 0x100


@dataclass(frozen=True)
class _Unit:
    x_slot: int
    y_slot: int

    @property
    def keys(self) -> int:
        return _KEY_OF_SLOT[self.x_slot] | _KEY_OF_SLOT[self.y_slot]


@dataclass(frozen=True)
class _Trigger:
    """A bit's handler: the unit it starts, its record (bit 5 clear) and bit 5's."""

    unit: int
    records: tuple[int, int]


@dataclass(frozen=True)
class _PsgDrum:
    divider: int
    first_tone: int               # the tone 3 attenuation the read writes
    noise: int
    envelope: int
    volume: int


@lru_cache(maxsize=8)
def read_sh2_drums(rom: RomImage) -> dict[int, FmDrum]:
    """Every drum byte's FmDrum ($81-$DF)."""
    z80 = driver_ram(rom)
    ram = Z80RamMemory(RomImage(z80))
    units = _units(z80)
    triggers = _triggers(z80)
    records = {r for t in triggers.values() for r in t.records}
    voices = {record: voice_registers(ram, ram.word(record + _VOICE)) for record in sorted(records)}
    resting = {reg: value for registers in voices.values() for reg, value in registers.items()}
    psg = _psg_drums(z80)
    envelopes = _psg_envelopes(rom, z80)

    drums = {}
    for byte in range(_FIRST_DRUM, _LAST_DRUM + 1):
        hits = _hits(byte, triggers)
        registers = dict(resting)
        for _, record in hits:
            registers.update(voices[record])
        frames = _fm_frames([(units[unit], ram.bytes_at(record, _RECORD)) for unit, record in hits])
        bit = next((b for b in _PSG_BITS if byte & b), _BIT0 if byte & _BIT2 else None)
        part = _psg_frames(psg[bit], envelopes, tone=not byte & _BIT3) if bit is not None else ()
        drums[byte] = FmDrum(voice_from_registers(0, registers), 0, frames, psg=part)
    return drums


def _hits(byte: int, triggers: dict[int, _Trigger]) -> list[tuple[int, int]]:
    """(unit, record) a drum byte starts, unit A's first: bit 5 picks a handler's second record."""
    second = 1 if byte & _BIT5 else 0
    hits = []
    if byte & (_BIT3 | _BIT0):
        trigger = triggers[_BIT3 if byte & _BIT3 else _BIT0]
        hits.append((trigger.unit, trigger.records[second]))
    if byte & _BIT2:
        trigger = triggers[_BIT2]
        hits.append((trigger.unit, trigger.records[1]))      # D6: the driver's, whatever bit 5 says
    return hits


def _fm_frames(hits: list[tuple[_Unit, bytes]]) -> tuple[FmFrame, ...]:
    """The units' operators frame by frame: written and keyed until their frames run out (a word
    stays where its last write left it)."""
    length = max((record[_FRAMES] for _, record in hits), default=0)
    out = []
    slots = [0, 0, 0, 0]
    for frame in range(length):
        keys = 0
        for unit, record in hits:
            if frame >= record[_FRAMES] - 1:
                continue                          # the count ran out: keyed off, no writes
            x_low = (record[_X_LOW] + frame * record[_X_STEP]) & 0xFF
            y_high = record[_Y_HIGH] if frame == 0 else (record[_Y_LOW] + record[_Y_STEP]) & 0xFF
            slots[_SLOT_INDEX[unit.x_slot]] = record[_X_HIGH] << 8 | x_low
            slots[_SLOT_INDEX[unit.y_slot]] = y_high << 8 | record[_Y_LOW]
            keys |= unit.keys
        out.append(FmFrame(slots[_SLOT_INDEX[_OWN_SLOT]], bool(keys), frame == 0 and bool(keys), tuple(slots), keys))
    return tuple(out)


def _psg_frames(part: _PsgDrum, envelopes: dict[int, tuple[int, ...] | None], tone: bool) -> tuple[PsgDrumFrame, ...]:
    """The PSG part from the read: each frame the envelope's step + the volume on the noise, and
    `tone`: tone 3 two steps quieter (else it keeps the read's); then silent at the mute."""
    steps = envelopes[part.envelope]
    if steps is None:
        raise RomError(f"PSG envelope {part.envelope}: holds or loops; a drum part that does is not read")
    out = []
    for step in steps:
        noise = min(_SILENT, step + part.volume)
        out.append(PsgDrumFrame(part.divider, part.noise, noise + _TONE_ABOVE_NOISE if tone else part.first_tone, noise))
    out.append(PsgDrumFrame(part.divider, part.noise, _SILENT, _SILENT))
    return tuple(out)


def _units(z80: bytes) -> dict[int, _Unit]:
    """Each unit (its RAM address) and the operator slots its frame update writes."""
    match = _one(_UPDATE, z80, "drum frame update")
    return {_word(match, 1): _unit(z80, _word(match, 2)), _word(match, 4): _unit(z80, _word(match, 3))}


def _unit(z80: bytes, registers: int) -> _Unit:
    """A unit's register list: op X's high and low, op Y's high and low."""
    _, x_low, _, y_low = z80[registers:registers + 4]
    return _Unit(_SLOT_OF_LOW_REGISTER[x_low], _SLOT_OF_LOW_REGISTER[y_low])


def _triggers(z80: bytes) -> dict[int, _Trigger]:
    """What bits 3, 0 and 2 each start."""
    dispatch = _one(_DISPATCH, z80, "drum read dispatch")
    out = {}
    for bit, group, pattern in ((_BIT3, 1, _HANDLER_A), (_BIT0, 2, _HANDLER_A), (_BIT2, 3, _HANDLER_B)):
        handler = _match_at(pattern, z80, _word(dispatch, group), "drum handler")
        out[bit] = _Trigger(_word(handler, 1), (_word(handler, 2), _word(handler, 3)))
    return out


def _psg_drums(z80: bytes) -> dict[int, _PsgDrum]:
    """The PSG part each of bits 3, 1 and 0 sets up (bit 2 plays bit 0's)."""
    dispatch = _one(_PSG_DISPATCH, z80, "PSG drum dispatch")
    targets = [dispatch.end(group) + int.from_bytes(dispatch.group(group), "little", signed=True) for group in (1, 2, 3)]
    out = {}
    for bit, target in zip(_PSG_BITS[:2], targets[:2], strict=True):
        m = _match_at(_PSG_TONE, z80, target, "PSG drum setup")
        out[bit] = _PsgDrum(_word(m, 1), _tone(_word(m, 2)), m.group(5)[0], m.group(3)[0], m.group(4)[0])
    m = _match_at(_PSG_NOISE, z80, targets[2], "PSG drum setup")
    out[_BIT0] = _PsgDrum(_word(m, 1), _tone(_word(m, 2)), m.group(3)[0], m.group(4)[0], m.group(5)[0])
    return out


def _tone(de: int) -> int:
    """The tone 3 attenuation in a setup's ld de (its low byte: $D0 | attenuation)."""
    return de & _SILENT


def _psg_envelopes(rom: RomImage, z80: bytes) -> dict[int, tuple[int, ...] | None]:
    """Each PSG envelope from 1, as attenuation steps to its mute (in the bank)."""
    memory = sh2_memory(rom)
    table = memory.rom_address(int.from_bytes(_one(_ENVELOPES, z80, "PSG envelope table").group(1), "little"))
    first = memory.header_pointer(table, table)
    return {i + 1: _envelope(memory, memory.header_pointer(table, table + i * _WORD))
            for i in range((first - table) // _WORD)}


def _envelope(memory: Sh2Memory, start: int) -> tuple[int, ...] | None:
    """Steps from `start` to the mute ($83); None: it holds ($81), restarts or jumps, sounding until
    the next read (no drum part's does)."""
    steps: list[int] = []
    for at in range(_MOST_STEPS):
        byte = memory.byte(start + at)
        if byte >= _COMMAND:
            return tuple(steps) if byte == _MUTE else None
        steps.append(byte)
    return None


def _one(pattern: re.Pattern[bytes], z80: bytes, what: str) -> re.Match[bytes]:
    found = list(pattern.finditer(z80))
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} {what}s, not one: not a Space Harrier II driver")
    return found[0]


def _match_at(pattern: re.Pattern[bytes], z80: bytes, at: int, what: str) -> re.Match[bytes]:
    match = pattern.match(z80, at)
    if match is None:
        raise RomError(f"Z80 ${at:04X}: no {what} there")
    return match


def _word(match: re.Match[bytes], group: int) -> int:
    return int.from_bytes(match.group(group), "little")
