"""Sonic 1's DAC samples, read out of the Kosinski-compressed Z80 driver.

    68k   lea (DACDriver).l,a0 / lea (z80_ram).l,a1      the blob, decompressed into Z80 RAM by KosDec
    Z80   the DPCM delta table (16 bytes)                each nibble adds one delta to an accumulator ($80)
          ld iy,zPCM_Table: 8 bytes a sample             start.w, size.w, pitch.b (little-endian)
    68k   subi.b #$88,d0 / move.b DAC_sample_rate(pc,d0.w),d0 / move.b d0,(z80_ram+zTimpani_Pitch).l
                                                         $88-$8B: the timpani at another pitch

The play loop takes 301 + 26 (pitch - 1) Z80 cycles a byte, which plays two samples:
rate = clock / (150.5 + 13 (pitch - 1)).  Kick 8200 Hz, snare 23784 Hz, timpani 7328 Hz.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chips import MD_PSG_CLOCK
from ..smps import SMPS_DAC_NAMES
from .image import RomError, RomImage
from .kosinski import kosinski

_Z80_CLOCK = MD_PSG_CLOCK                 # the Z80 and the PSG both run at the master clock / 15
_CYCLES_PER_SAMPLE = 150.5
_CYCLES_PER_PITCH = 13                    # one djnz turn

# The 68k code that finds the blob: lea (DACDriver).l,a0 / lea (z80_ram).l,a1
_LEA_A0 = bytes.fromhex("41F9")
_LEA_Z80_RAM_A1 = bytes.fromhex("43F900A00000")
_LONG = 4

# JMan2050's delta table, as Sonic 1's driver stores it
_DELTAS = bytes([0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
                 0x80, 0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE0, 0xC0])
_ACCUMULATOR = 0x80
_LD_IY = bytes.fromhex("FD21")            # ld iy,nn: the PCM table
_ENTRY = 8
_ENTRY_SIZE, _ENTRY_PITCH = 2, 4

# Which samples the table holds, and the timpani's pitch table on the 68k side
_FIRST_SAMPLE = 0x81
_TIMPANI = 0x83
_FIRST_PITCHED = 0x88
_MOVE_TO_TIMPANI_PITCH = bytes.fromhex("13C000A0")     # move.b d0,(z80_ram + ...).l
_MOVE_PC_INDEXED = bytes.fromhex("103B")               # move.b d8(pc,d0.w),d0
_PC_SEARCH = 0x40                                       # how far before the write the read sits


@dataclass(frozen=True)
class DacSample:
    sound: int        # the DAC track's byte: $81 dKick
    name: str
    pcm: bytes        # signed 8-bit, as samples/*.raw hold it
    pitch: int        # the play loop's counter
    rate: float       # Hz

    @property
    def is_pitched_copy(self) -> bool:
        """$88-$8B: the timpani's bytes at another pitch."""
        return self.sound >= _FIRST_PITCHED


def dac_samples(rom: RomImage) -> list[DacSample]:
    """Every DAC sample a song can play, $81 dKick ... $8B dVLowTimpani."""
    z80 = _z80_driver(rom)
    table = _pcm_table(z80)
    deltas_at = z80.find(_DELTAS)
    if deltas_at < 0:
        raise RomError("Z80 driver: no DPCM delta table")

    samples: dict[int, DacSample] = {}
    for sound, name in sorted((v, k) for k, v in SMPS_DAC_NAMES.items() if v < _FIRST_PITCHED):
        at = table + (sound - _FIRST_SAMPLE) * _ENTRY
        start, size, pitch = _word(z80, at), _word(z80, at + _ENTRY_SIZE), z80[at + _ENTRY_PITCH]
        samples[sound] = DacSample(sound, name, _decode(z80[start:start + size]), pitch, _rate(pitch))

    timpani = samples[_TIMPANI]
    pitches = _timpani_pitches(rom, table)
    for sound, name in sorted((v, k) for k, v in SMPS_DAC_NAMES.items() if v >= _FIRST_PITCHED):
        pitch = pitches[sound - _FIRST_PITCHED]
        samples[sound] = DacSample(sound, name, timpani.pcm, pitch, _rate(pitch))
    return list(samples.values())


def _z80_driver(rom: RomImage) -> bytes:
    """The Z80 driver, found through the 68k code that loads it."""
    found = [a for a in rom.find_all(_LEA_Z80_RAM_A1) if rom.bytes_at(a - _LONG - len(_LEA_A0), 2) == _LEA_A0]
    if len(found) != 1:
        raise RomError(f"{len(found)} loads of the Z80 driver (lea x,a0 / lea z80_ram,a1), not one")
    blob, _ = kosinski(rom.data, rom.long(found[0] - _LONG))
    return blob


def _pcm_table(z80: bytes) -> int:
    at = z80.find(_LD_IY)
    if at < 0:
        raise RomError("Z80 driver: no PCM table (ld iy,nn)")
    return _word(z80, at + len(_LD_IY))


def _timpani_pitches(rom: RomImage, table: int) -> bytes:
    """DAC_sample_rate: the pitch the 68k writes into the timpani's entry for $88 onwards."""
    pitch_entry = table + (_TIMPANI - _FIRST_SAMPLE) * _ENTRY + _ENTRY_PITCH
    writes = rom.find_all(_MOVE_TO_TIMPANI_PITCH + pitch_entry.to_bytes(2, "big"))
    if len(writes) != 1:
        raise RomError(f"{len(writes)} writes of the timpani's pitch, not one")

    read = rom.data.rfind(_MOVE_PC_INDEXED, writes[0] - _PC_SEARCH, writes[0])
    if read < 0:
        raise RomError("no DAC_sample_rate read before the timpani's pitch write")
    extension = read + len(_MOVE_PC_INDEXED)
    displacement = rom.byte(extension + 1)
    start = extension + (displacement - 0x100 if displacement > 0x7F else displacement)
    count = sum(1 for v in SMPS_DAC_NAMES.values() if v >= _FIRST_PITCHED)
    return rom.bytes_at(start, count)


def _decode(dpcm: bytes) -> bytes:
    """Each byte's high nibble, then its low one, each a delta on the accumulator; signed out."""
    acc, out = _ACCUMULATOR, bytearray()
    for byte in dpcm:
        for nibble in (byte >> 4, byte & 0xF):
            acc = (acc + _DELTAS[nibble]) & 0xFF
            out.append(acc ^ 0x80)
    return bytes(out)


def _rate(pitch: int) -> float:
    return _Z80_CLOCK / (_CYCLES_PER_SAMPLE + _CYCLES_PER_PITCH * (pitch - 1))


def _word(data: bytes, at: int) -> int:
    return data[at] | data[at + 1] << 8
