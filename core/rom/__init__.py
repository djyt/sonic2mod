"""Mega Drive ROMs: SMPS songs read from their bytecode (the score itself), beside vgm/.

    song.py  detect.py       what a ROM holds: its index, each sound -> SongCode / SmpsSong, its
                             DAC samples; the variant pinned by SHA-1, else the one that reads it
    variants.py              every variant, the ROMs each is known in, their data fixes
    smps68k/                 the 68k family: Sonic 1 (Type 1b), Moonwalker (Type 1a)
    header.py  tracks.py     generic readers: headers, track bytes -> SmpsCode (the ops the asm
    voices.py  envelopes.py  parser makes from macros), voices, PSG envelopes; driven by the variant
    variant.py  flags.py     the vocabulary: SmpsVariant, VoiceLayout, SoundIndex, DacSample;
    memory.py  fixes.py      flag specs; SoundMemory (how a driver reads pointers); RomFix
    image.py                 RomImage: the header, big-endian reads by address

    image / memory / flags / fixes / variant  <-  readers  <-  families  <-  variants  <-  detect / song
"""

from .detect import detect_variant
from .fixes import RomFix
from .header import track_label
from .image import RomError, RomImage, is_rom_path
from .song import dac_samples, locate_sounds, read_rom_code, read_rom_song
from .variant import DacSample, SmpsVariant, SoundIndex
from .variants import VARIANTS, data_fixes

__all__ = [
    "VARIANTS",
    "DacSample",
    "RomError",
    "RomFix",
    "RomImage",
    "SmpsVariant",
    "SoundIndex",
    "dac_samples",
    "data_fixes",
    "detect_variant",
    "is_rom_path",
    "locate_sounds",
    "read_rom_code",
    "read_rom_song",
    "track_label",
]
