"""Which variant a ROM's songs are read with.

A ROM known by its SHA-1 (games.py) reads with its game's driver.  Any other is tried with every driver: the
one that locates the songs and under which every song decodes - each flag known, no code starting
inside an instruction, every pointer inside the ROM - is it.  None, or more than one, stops with
what each attempt hit.  (Sonic 1's songs fail under Type 1a at the first $E3 return; 8 of
Moonwalker's 23 under Sonic 1's.)
"""

from __future__ import annotations

from functools import lru_cache

from core.rom.header import music_header
from core.rom.image import RomError, RomImage
from core.rom.tracks import decode_tracks
from core.rom.variant import SmpsVariant, SoundIndex

from .games import known_game
from .registry import all_drivers, load_driver


@lru_cache(maxsize=8)
def detect_variant(rom: RomImage) -> SmpsVariant:
    game = known_game(rom)
    if game is not None:
        return load_driver(game.driver)

    drivers = all_drivers()
    failures = {driver.name: _failure(rom, driver) for driver in drivers}
    fits = [driver for driver in drivers if failures[driver.name] is None]
    if len(fits) == 1:
        return fits[0]

    tried = "; ".join(f"{name}: {failure or 'reads every song'}" for name, failure in failures.items())
    raise RomError(f"no single driver reads this ROM's songs ({tried}); state driver: in the config")


def first_failure(rom: RomImage, index: SoundIndex, variant: SmpsVariant) -> str | None:
    """What stops `variant` reading the ROM's songs, or None when it reads them all."""
    memory = variant.memory(rom)
    for sound_id, address in sorted(index.music.items()):
        try:
            decode_tracks(memory, music_header(memory, address, variant).tracks, variant)
        except RomError as e:
            return f"${sound_id:02X}: {e}"
    return None


def _failure(rom: RomImage, variant: SmpsVariant) -> str | None:
    try:
        index = variant.locate(rom)
    except RomError as e:
        return str(e)
    return first_failure(rom, index, variant)
