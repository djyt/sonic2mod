"""Mega Drive ROMs: the framework SMPS songs are read from their bytecode with (the score itself),
beside vgm/.  It knows no driver: each is a description in core/drivers, the layer above, that
these readers are driven by.

    header.py  tracks.py     generic readers: headers, track bytes -> SmpsCode (the ops the asm
    voices.py  envelopes.py  parser makes from macros), voices, PSG envelopes; driven by the variant
    grammar.py               a track grammar: the instruction at an address (SMPS's)
    z80.py  kosinski.py      Z80 RAM as the 68k loads it
    variant.py  flags.py     the vocabulary: SmpsVariant, VoiceLayout, SoundIndex, DacSample;
    memory.py  fixes.py      flag specs; SoundMemory (how a driver reads pointers); RomFix
    image.py                 RomImage: the header, big-endian reads by address

    image / memory / flags / fixes / grammar / variant  <-  readers
"""

from .fixes import RomFix
from .image import RomError, RomImage, is_rom_path
from .variant import DacSample, SmpsVariant, SoundIndex

__all__ = [
    "DacSample",
    "RomError",
    "RomFix",
    "RomImage",
    "SmpsVariant",
    "SoundIndex",
    "is_rom_path",
]
