"""Which driver a ROM's songs are read with.

A ROM known by its SHA-1 is pinned.  Any other is tried with every driver drivers.py knows: the
one under which every song decodes - each flag known, no code starting inside an instruction,
every pointer inside the ROM - is it.  None, or more than one, stops with what each attempt hit.
(Sonic 1's songs fail under Type 1a at the first $E3 return; 8 of Moonwalker's 23 under Sonic 1's.)
"""

from __future__ import annotations

from functools import lru_cache

from .drivers import DRIVERS, SONIC1, TYPE1A, RomDriver
from .fixes import SONIC1_REV01_SHA1
from .header import read_music_header
from .image import RomError, RomImage
from .locate import SoundIndex, locate_sounds
from .tracks import decode_tracks

MOONWALKER_REV_A_SHA1 = "70d9b760c87196af364492512104fa18c9d69cce"

_PINNED: dict[str, RomDriver] = {
    SONIC1_REV01_SHA1: SONIC1,
    MOONWALKER_REV_A_SHA1: TYPE1A,
}


@lru_cache(maxsize=8)
def detect_driver(rom: RomImage) -> RomDriver:
    pinned = _PINNED.get(rom.sha1)
    if pinned is not None:
        return pinned

    index = locate_sounds(rom)
    failures = {driver.name: first_failure(rom, index, driver) for driver in DRIVERS.values()}
    fits = [DRIVERS[name] for name, failure in failures.items() if failure is None]
    if len(fits) == 1:
        return fits[0]

    tried = "; ".join(f"{name}: {failure or 'reads every song'}" for name, failure in failures.items())
    raise RomError(f"no single driver reads this ROM's songs ({tried}); state driver: in the config")


def first_failure(rom: RomImage, index: SoundIndex, driver: RomDriver) -> str | None:
    """What stops `driver` reading the ROM's songs, or None when it reads them all."""
    for sound_id, address in sorted(index.music.items()):
        try:
            decode_tracks(rom, read_music_header(rom, address).tracks, driver=driver)
        except RomError as e:
            return f"${sound_id:02X}: {e}"
    return None
