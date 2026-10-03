"""Mega Drive ROMs: SMPS songs read from their bytecode (the score itself), beside vgm/.

    image.py    RomImage: the header, big-endian reads by address
    locate.py   locate_sounds: the driver's song and SFX indexes (Sonic 1's driver)
    header.py   song / SFX headers -> SmpsSongHeader, each track's address
    tracks.py   track bytes -> SmpsCode (the ops the asm parser makes from macros)
    voices.py   the voice bank -> SmpsVoice
    song.py     read_rom_song: a sound ID -> SmpsSong (read_rom_code: before the walk)
    fixes.py    data_fixes: the disassembly's FixMusicAndSFXDataBugs as byte edits, for the ROM they are known in
    dac.py      dac_samples: the DPCM samples in the Kosinski-compressed Z80 driver (kosinski.py)
"""

from .dac import DacSample, dac_samples
from .fixes import RomFix, data_fixes
from .header import track_label
from .image import RomError, RomImage, is_rom_path
from .locate import FIRST_MUSIC, FIRST_SFX, FIRST_SPECIAL_SFX, SoundIndex, locate_sounds
from .song import read_rom_code, read_rom_song

__all__ = [
    "FIRST_MUSIC",
    "FIRST_SFX",
    "FIRST_SPECIAL_SFX",
    "DacSample",
    "RomError",
    "RomFix",
    "RomImage",
    "SoundIndex",
    "dac_samples",
    "data_fixes",
    "is_rom_path",
    "locate_sounds",
    "read_rom_code",
    "read_rom_song",
    "track_label",
]
