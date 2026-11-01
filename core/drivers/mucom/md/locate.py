"""Where Mucom-MD keeps its songs and tables: each found by the code that reads it, a `lea (pc)`
and the instructions after it (one table however many places read it).

    song index   lea (index,pc),a0 / andi.w #$FF,d0 / lsl.w #2,d0 / move.l (a0,d0.w),d1
                 longs from the index: songs $00-$1B, then empty longs, SFX from $20
    FM octave    12 words C-B, the block the note's octave            (Streets of Rage's words)
    FM volume    20 carrier TLs by volume step 0-19
    PSG octave   12 words C-B, the dividers x 8: a note's divider is its word >> its octave
    PSG presets  16 envelopes x 6 bytes (AL AR DR SL SR RR)               (Phase 3: not read yet)

    Streets of Rage 2 (USA)   index $429CE; tables $4291A $4293E $42956 $4296E
"""

from __future__ import annotations

import re
from functools import lru_cache

from core.chips import freq_word
from core.rom.header import read_index
from core.rom.image import RomError, RomImage
from core.rom.pattern import code_pattern
from core.rom.variant import SoundIndex

from .header import is_image
from .memory import MucomMemory

_LONG, _WORD = 4, 2
_SEMITONES = 12
_OCTAVES = 8
_FIRST_SFX = 0x20              # the SFX's IDs: the index's slots from $20
_MAX_SOUNDS = 0x80

_SONG_INDEX = code_pattern("""
    41 FA <w>          lea     (index,pc),a0
    02 40 00 FF        andi.w  #$FF,d0
    E5 48              lsl.w   #2,d0
    22 30 00 00        move.l  (a0,d0.w),d1
""")
_FM_OCTAVE = code_pattern("""
    45 FA <w>          lea     (octave,pc),a2
    32 32 00 00        move.w  (a2,d0.w),d1
    02 43 00 F0        andi.w  #$F0,d3
""")
_FM_VOLUME = code_pattern("""
    43 FA <w>          lea     (volume,pc),a1
    10 31 00 00        move.b  (a1,d0.w),d0
    1D 40 00 15        move.b  d0,$15(a6)
""")
_PSG_OCTAVE = code_pattern("""
    45 FA <w>          lea     (octave,pc),a2
    02 03 00 0F        andi.b  #$F,d3
    D6 43              add.w   d3,d3
""")

FM_VOLUME_STEPS = 20           # TL by step 0-19
PSG_MAX_DIVIDER = 0x3FF        # the tone register's 10 bits: a larger divider is clamped


def pc_table(rom: RomImage, pattern: re.Pattern[bytes]) -> int:
    """The one table every match of `pattern` loads (its `lea (d16,pc)` first, the word captured)."""
    targets = {m.start() + _WORD + rom.signed_word(m.start() + _WORD) for m in pattern.finditer(rom.data)}
    if len(targets) != 1:
        raise RomError(f"{len(targets)} tables read by the code {pattern.pattern!r}, not one: not Mucom-MD")
    return targets.pop()


@lru_cache(maxsize=8)
def locate(rom: RomImage) -> SoundIndex:
    """The song and SFX indexes: relative longs, a slot of 0 empty."""
    table = pc_table(rom, _SONG_INDEX)
    memory = MucomMemory(rom)

    def pointer(slot: int) -> int:
        return table + rom.long(slot)

    slots = range(table, table + _MAX_SOUNDS * _LONG, _LONG)
    music = read_index(memory, slots, pointer, 0, is_image)
    sfx = read_index(memory, slots[_FIRST_SFX:], pointer, _FIRST_SFX, is_image)
    return SoundIndex(music, sfx)


def fm_frequencies(rom: RomImage) -> tuple[int, ...]:
    """The FM frequency words by fm_note_index (1 = C0): the octave's words at blocks 0-7."""
    octave = _words(rom, pc_table(rom, _FM_OCTAVE))
    return (0, *[freq_word(word, block) for block in range(_OCTAVES) for word in octave])


def fm_volumes(rom: RomImage) -> tuple[int, ...]:
    """The carriers' TL by volume step 0-19."""
    return tuple(rom.bytes_at(pc_table(rom, _FM_VOLUME), FM_VOLUME_STEPS))


def psg_octave(rom: RomImage) -> tuple[int, ...]:
    """The PSG's 12 words C-B: a note's divider is its word >> its octave."""
    return _words(rom, pc_table(rom, _PSG_OCTAVE))


def _words(rom: RomImage, at: int) -> tuple[int, ...]:
    return tuple(rom.word(at + i * _WORD) for i in range(_SEMITONES))

