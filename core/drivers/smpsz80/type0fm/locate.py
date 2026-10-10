"""Where an SMPS Z80 Type 0 FM driver keeps its sounds - found by the shape of its tables, not by
one game's bytes.

    driver     the Z80 program holding an FM table (../program.py)
    bank       the 32 KB ROM bank the driver maps at Z80 $8000, its sound header at the start
    header     words: +0 priorities  +4 music index  +6 SFX index  +8 pitch envelopes
    indexes    a word per sound: music from $81, SFX $90-$B9 (the driver's queue dispatch)

    Golden Axe (Rev A)   driver ROM $1D2F0 -> Z80 $0000, FM table Z80 $07D9; bank $18000,
                         music $8069 (15), SFX $8087 (42)

No PSG volume envelope table is located: no Type 0 FM song uses the PSG.
"""

from __future__ import annotations

from functools import lru_cache, partial

from core.rom.header import is_index, is_music_header, is_sfx_header, read_index
from core.rom.image import RomError, RomImage
from core.rom.variant import SoundIndex

from ..memory import BANK_SIZE, BankedZ80Memory
from ..program import driver_ram
from .layout import HEADER_TYPE0

_FIRST_MUSIC = 0x81
_FIRST_SFX = 0x90            # the dispatch: $81-$8F music, $90-$B9 SFX, $E0-$E3 commands
_LAST_SFX = 0xB9

_MUSIC_INDEX = 4             # the sound header's words
_SFX_INDEX = 6
_WORD = 2
_ENTRIES_CHECKED = 3


@lru_cache(maxsize=8)
def locate_type0(rom: RomImage) -> SoundIndex:
    """The music and SFX indexes of a ROM with an SMPS Z80 Type 0 FM driver."""
    bank = sound_bank(rom)
    memory = BankedZ80Memory(rom, bank)
    music_table = memory.header_pointer(bank, bank + _MUSIC_INDEX)
    sfx_table = memory.header_pointer(bank, bank + _SFX_INDEX)

    def index(start: int, end: int, first_id: int, most: int, plausible) -> dict[int, int]:
        """A word per sound: up to `end`, `most` of them."""
        slots = range(start, min(end, start + most * _WORD), _WORD)
        return read_index(memory, slots, lambda at: memory.header_pointer(start, at), first_id, plausible)

    music = index(music_table, sfx_table, _FIRST_MUSIC, _FIRST_SFX - _FIRST_MUSIC, _is_music_header)
    sfx = index(sfx_table, bank + BANK_SIZE, _FIRST_SFX, _LAST_SFX - _FIRST_SFX + 1, _is_sfx_header)
    return SoundIndex(music, sfx)


@lru_cache(maxsize=8)
def sound_bank(rom: RomImage) -> int:
    """The one bank whose start is a sound header, behind a Z80 driver with an FM table."""
    driver_ram(rom)
    found = [bank for bank in range(0, len(rom.data), BANK_SIZE) if _is_sound_header(BankedZ80Memory(rom, bank), bank)]
    if len(found) != 1:
        where = ", ".join(f"${b:X}" for b in found)
        raise RomError(f"{len(found)} banks start with a sound header{': ' + where if where else ''}, not one")
    return found[0]


def _is_sound_header(memory: BankedZ80Memory, bank: int) -> bool:
    """The music and SFX indexes it names start with plausible headers."""
    if not memory.contains(bank, _SFX_INDEX + _WORD):
        return False
    music_table = memory.header_pointer(bank, bank + _MUSIC_INDEX)
    sfx_table = memory.header_pointer(bank, bank + _SFX_INDEX)
    def starts_index(table: int, plausible) -> bool:
        slots = range(table, table + _ENTRIES_CHECKED * _WORD, _WORD)
        return memory.contains(table, len(slots) * _WORD) and is_index(
            memory, slots, lambda at: memory.header_pointer(table, at), plausible)

    return starts_index(music_table, _is_music_header) and starts_index(sfx_table, _is_sfx_header)


_is_music_header = partial(is_music_header, layout=HEADER_TYPE0)
_is_sfx_header = partial(is_sfx_header, layout=HEADER_TYPE0)

