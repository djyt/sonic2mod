"""What a ROM's sound driver holds: its sound index, each song or sound effect (header, track code
and voices -> SmpsSong) and its DAC samples.  The variant is detected unless given."""

from __future__ import annotations

from ..smps import SmpsSong, SongCode
from .detect import detect_variant
from .envelopes import read_envelopes
from .fixes import apply_fixes
from .header import read_music_header, read_sfx_header
from .image import RomImage
from .tracks import decode_tracks
from .variant import DacSample, SmpsVariant, SoundIndex
from .variants import data_fixes
from .voices import read_voices, voices_used


def locate_sounds(rom: RomImage, variant: SmpsVariant | None = None) -> SoundIndex:
    """Each song's and SFX's header address, and the PSG envelopes'."""
    return (variant or detect_variant(rom)).locate(rom)


def dac_samples(rom: RomImage, variant: SmpsVariant | None = None) -> list[DacSample]:
    """Every DAC sample a song can play, its pitched copies after the samples."""
    variant = variant or detect_variant(rom)
    return variant.dac(rom, variant.dac_names)


def read_rom_code(rom: RomImage, sound_id: int, index: SoundIndex | None = None,
                  fix_data_bugs: bool = True, variant: SmpsVariant | None = None) -> SongCode:
    """Sound `sound_id` ($81 GHZ ... $93; SFX from $A0) before the walk.  `index`: the ROM's,
    when already located.  fix_data_bugs: the fixes known for this exact ROM (fixes.py), as the
    asm parser applies the disassembly's; False reads the game as shipped."""
    variant = variant or detect_variant(rom)
    index = index or variant.locate(rom)
    address = index.address(sound_id)
    image, splices = apply_fixes(rom, data_fixes(rom) if fix_data_bugs else ())
    memory = variant.memory(image)
    read_header = read_sfx_header if index.is_sfx(sound_id) else read_music_header
    head = read_header(memory, address, variant.header)

    tracks = decode_tracks(memory, head.tracks, variant, splices)
    voices = []
    if head.voices is not None:
        voices = read_voices(memory, head.voices, voices_used(tracks.code), variant.voice_layout)
        tracks.labels[head.header.voice_label] = head.voices
    envelopes = read_envelopes(memory, index.envelopes, variant) if index.envelopes else None
    return SongCode(head.header, tracks.code, voices, address=address, addresses=tracks.labels,
                    driver=variant.name, dropped=dict(tracks.dropped), psg_envelopes=envelopes,
                    dac_names=dict(variant.dac_names))


def read_rom_song(rom: RomImage, sound_id: int, index: SoundIndex | None = None,
                  fix_data_bugs: bool = True, variant: SmpsVariant | None = None) -> SmpsSong:
    """Sound `sound_id` as SmpsParser reads its asm (fix_data_bugs alike)."""
    return read_rom_code(rom, sound_id, index, fix_data_bugs, variant).song()
