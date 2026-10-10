"""OutRun's DAC samples: a table at the start of the DAC bank, read by the drum track's note (Z80
$01DC) and played between Timer A polls by the main loop ($0082).

    table    the bank's Z80 $8000 (ld hl,TABLE after sub $81 / ret m): 6 bytes a sample from $81 -
             pointer.w (in the bank's window), length.w, rate.b (the djnz count), flags.b; as
             many as the first pointer leaves room for (13)
    flags    bit 0: raw 8-bit PCM ($0092: the loop at $0136), else Sonic 1's DPCM ($009D, the delta
             table at Z80 $0063); bits 4-7 written to B6 (FM6: L R AMS) as the sample starts
             ($0189): each sample's own pan (dac_pans)
    loop     cycles an output, the YM not busy: DPCM 160 + 13 x rate (a byte's two nibbles: 151 +
             13 x rate, 169 + 13 x rate), PCM 171 + 13 x rate; one Timer A poll a byte
    empty    a length of 0 ($87, $89) plays nothing a song names: not read

The update runs inside the loop (the poll calls it): the rips play each sample slower than its
loop by 33-50 % (docs/todo/done/outrun.md 3.1).
"""

from __future__ import annotations

from collections.abc import Mapping

from core.rom.image import RomImage
from core.rom.pattern import code_pattern
from core.rom.pcm import Dpcm, Pcm, PcmEntry, PcmTable, SampleFormat, delta_table
from core.rom.variant import DacSample
from core.rom.z80 import z80_word
from core.smps import FIRST_NOTE

from ..memory import window_view
from ..program import driver_ram, one_match
from .locate import driver_tables

_DRIVER = "OutRun"
_ENTRY = PcmEntry(6, size_at=2, pitch_at=4)
_FLAGS_AT = 5
_RAW_PCM = 0x01
_PAN_BITS = 0xF0

# Cycles an output at rate 1, and each rate step (one djnz turn)
_DPCM_CYCLES = (173, 13)
_PCM_CYCLES = (184, 13)

_SAMPLE_START = code_pattern("""
    3A ? ?      ld a,(note)         the drum track's note byte
    D6 81       sub $81
    F8          ret m
    21 <w>      ld hl,table
    87          add a,a
    4F          ld c,a
    87          add a,a
    81          add a,c             x 6
""")


def outrun_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """Every sample the table holds bytes for, decoded, at its loop's rate."""
    table, memory = _table(rom)
    dpcm = Dpcm(delta_table(driver_ram(rom)), *_DPCM_CYCLES)
    pcm = Pcm(*_PCM_CYCLES)

    def format_of(entry: bytes) -> SampleFormat:
        return pcm if entry[_FLAGS_AT] & _RAW_PCM else dpcm

    samples = PcmTable(memory, table, _ENTRY, format_of, names)
    return [samples.sample(sound) for sound in _sounds(memory, table) if _size(memory, table, sound)]


def dac_pans(rom: RomImage) -> dict[int, int]:
    """Each sample's B4 byte (L R AMS), by the drum track's byte."""
    table, memory = _table(rom)
    return {sound: memory[_entry_at(table, sound) + _FLAGS_AT] & _PAN_BITS for sound in _sounds(memory, table)}


def _table(rom: RomImage) -> tuple[int, bytes]:
    """The table's Z80 address, and the Z80's view of the DAC bank (its window at $8000)."""
    memory = window_view(rom, driver_tables(rom).dac_bank)
    table = z80_word(one_match(_SAMPLE_START, driver_ram(rom), "DAC sample start", _DRIVER).group(1), 0)
    return table, memory


def _sounds(memory: bytes, table: int) -> range:
    """The table's sounds: up to its first sample's bytes."""
    return range(FIRST_NOTE, FIRST_NOTE + (z80_word(memory, table) - table) // _ENTRY.size)


def _size(memory: bytes, table: int, sound: int) -> int:
    return z80_word(memory, _entry_at(table, sound) + _ENTRY.size_at)


def _entry_at(table: int, sound: int) -> int:
    return table + (sound - FIRST_NOTE) * _ENTRY.size
