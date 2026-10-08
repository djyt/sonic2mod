"""Which variant a ROM's songs are read with.

A ROM known by its SHA-1 is pinned.  Any other is tried with every variant variants.py knows: the
one that locates the songs and under which every song decodes - each flag known, no code starting
inside an instruction, every pointer inside the ROM - is it.  None, or more than one, stops with
what each attempt hit.  (Sonic 1's songs fail under Type 1a at the first $E3 return; 8 of
Moonwalker's 23 under Sonic 1's.)
"""

from __future__ import annotations

from functools import lru_cache

from core.rom.header import read_music_header
from core.rom.image import RomError, RomImage
from core.rom.tracks import decode_tracks
from core.rom.variant import SmpsVariant, SoundIndex

from .registry import VARIANTS, pinned_variant


@lru_cache(maxsize=8)
def detect_variant(rom: RomImage) -> SmpsVariant:
    pinned = pinned_variant(rom)
    if pinned is not None:
        return pinned

    failures = {variant.name: _failure(rom, variant) for variant in VARIANTS.values()}
    fits = [VARIANTS[name] for name, failure in failures.items() if failure is None]
    if len(fits) == 1:
        return fits[0]

    tried = "; ".join(f"{name}: {failure or 'reads every song'}" for name, failure in failures.items())
    raise RomError(f"no single driver reads this ROM's songs ({tried}); state driver: in the config")


def first_failure(rom: RomImage, index: SoundIndex, variant: SmpsVariant) -> str | None:
    """What stops `variant` reading the ROM's songs, or None when it reads them all."""
    memory = variant.memory(rom)
    for sound_id, address in sorted(index.music.items()):
        try:
            decode_tracks(memory, read_music_header(memory, address, variant.header).tracks, variant)
        except RomError as e:
            return f"${sound_id:02X}: {e}"
    return None


def _failure(rom: RomImage, variant: SmpsVariant) -> str | None:
    try:
        index = variant.locate(rom)
    except RomError as e:
        return str(e)
    return first_failure(rom, index, variant)
