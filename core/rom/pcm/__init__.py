"""DAC samples as a Z80 player stores them: the table that indexes them (an entry per sound, from
$81) and the format each is stored in.  Each driver finds its table, and lays out and plays its
samples its own way (the drivers' dac.py); the memory the table is read from is the Z80's: its RAM
as the 68k loads it (core/rom/z80.py), or a ROM bank in its window.

    __init__.py   the table, what any format is, where a player keeps its table:
                  PcmTable      start.w at +0, size.w and pitch.b where PcmEntry says; each
                                entry's format chosen from its bytes
                  SampleFormat  a sample's bytes -> signed PCM, and the Z80 cycles each output takes
                  pcm_table     a player descended from Sonic 1's: its `ld iy,nn`
    smps.py       Sonic 1's player and its descendants (Moonwalker, OutRun): Dpcm, Pcm, delta_table
    sor.py        Streets of Rage's players (its sequel's too): RunDpcm
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from core.chips import MD_PSG_CLOCK
from core.smps import FIRST_NOTE

from ..image import RomError
from ..variant import DacSample
from ..z80 import z80_word

_Z80_CLOCK = MD_PSG_CLOCK                 # the Z80 and the PSG both run at the master clock / 15
ACCUMULATOR = 0x80                        # where a sample's output starts
SIGN = 0x80                               # the Z80's unsigned byte -> signed PCM
_LD_IY = bytes.fromhex("FD21")            # ld iy,nn: a player's PCM table


class SampleFormat(Protocol):
    """How a Z80 player stores and plays a sample."""

    def decode(self, data: bytes) -> bytes:
        """The sample's stored bytes as signed 8-bit PCM."""
        ...

    def cycles(self, data: bytes, pitch: int, /) -> float:
        """Z80 cycles per output, on average, played at `pitch`."""
        ...


@dataclass(frozen=True)
class PcmEntry:
    """A PCM table entry: `size` bytes, the start word at +0, the size word at `size_at`, the
    pitch byte at `pitch_at` (little-endian)."""

    size: int
    size_at: int
    pitch_at: int


# The format an entry's bytes say its sample is stored in
FormatOf = Callable[[bytes], SampleFormat]


def one_format(form: SampleFormat) -> FormatOf:
    """Every entry's sample in `form`."""
    return lambda _entry: form


class PcmTable:
    """A Z80 player's samples, an entry each from sound $81 at `at` in `memory` (Z80 addresses)."""

    def __init__(self, memory: bytes, at: int, entry: PcmEntry, format_of: FormatOf, names: Mapping[int, str]):
        self._memory = memory
        self._at = at
        self._entry = entry
        self._format_of = format_of
        self._names = names

    def sample(self, sound: int) -> DacSample:
        data = self._data(sound)
        pitch = self._memory[self.pitch_address(sound)]
        return DacSample(sound, self._names[sound], self._format(sound).decode(data), pitch, self._rate(sound, pitch))

    def pitched_copy(self, of: DacSample, sound: int, pitch: int) -> DacSample:
        """Sound `sound`: `of`'s sample at another pitch."""
        return DacSample(sound, self._names[sound], of.pcm, pitch, self._rate(of.sound, pitch), of.sound)

    def pitch_address(self, sound: int) -> int:
        """Where `sound`'s pitch byte is (what the 68k writes another pitch to)."""
        return self._address(sound) + self._entry.pitch_at

    def _data(self, sound: int) -> bytes:
        at = self._address(sound)
        start, size = z80_word(self._memory, at), z80_word(self._memory, at + self._entry.size_at)
        if not size or start + size > len(self._memory):
            raise RomError(f"DAC sample ${sound:02X}: entry at Z80 ${at:04X} holds no sample")
        return self._memory[start:start + size]

    def _format(self, sound: int) -> SampleFormat:
        at = self._address(sound)
        return self._format_of(self._memory[at:at + self._entry.size])

    def _address(self, sound: int) -> int:
        return self._at + (sound - FIRST_NOTE) * self._entry.size

    def _rate(self, sound: int, pitch: int) -> float:
        return _Z80_CLOCK / self._format(sound).cycles(self._data(sound), pitch)


def pcm_table(z80: bytes) -> int:
    """The Z80 address of the player's PCM table: the one `ld iy,nn` of a player descended from
    Sonic 1's (Moonwalker's, Streets of Rage's and its sequel's)."""
    at = z80.find(_LD_IY)
    if at < 0:
        raise RomError("Z80 driver: no PCM table (ld iy,nn)")
    return z80_word(z80, at + len(_LD_IY))
