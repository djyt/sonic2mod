"""What a ROM's sound driver holds: its sound index, each song or sound effect (header, track code
and voices -> SmpsSong) and its DAC samples.  The variant is detected unless given."""

from __future__ import annotations

from dataclasses import replace

from core.rom.envelopes import read_envelopes
from core.rom.fixes import apply_fixes
from core.rom.header import read_music_header, read_sfx_header
from core.rom.image import RomImage
from core.rom.memory import SoundMemory
from core.rom.tracks import decode_tracks
from core.rom.variant import DacSample, SmpsVariant, SoundIndex
from core.rom.voices import read_voices, voices_used
from core.smps import PlaybackRules, SmpsSong, SongCode

from .detect import detect_variant
from .games import data_fixes


def locate_sounds(rom: RomImage, variant: SmpsVariant | None = None) -> SoundIndex:
    """Each song's and SFX's header address, and the PSG envelopes'."""
    return (variant or detect_variant(rom)).locate(rom)


def dac_samples(rom: RomImage, variant: SmpsVariant | None = None) -> list[DacSample]:
    """Every DAC sample a song can play, its pitched copies after the samples."""
    variant = variant or detect_variant(rom)
    return variant.dac(rom, variant.rules.dac_names) if variant.dac else []


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
    rules = _rules(variant, image, memory, index)
    drums = {}
    if variant.fm_drums and not index.is_sfx(sound_id):
        drums = variant.fm_drums(image, head.header, rules.fm_frequencies)
    return SongCode(head.header, tracks.code, voices, rules, address=address, addresses=tracks.labels,
                    dropped=dict(tracks.dropped), fm_drums=drums)


def _rules(variant: SmpsVariant, image: RomImage, memory: SoundMemory, index: SoundIndex) -> PlaybackRules:
    """The variant's rules, with what this ROM's driver holds read from it: its tables, its PSG
    envelopes."""
    rules = variant.rules
    if variant.rules_from_rom:
        rules = variant.rules_from_rom(image, rules)
    if index.envelopes:
        rules = replace(rules, psg_envelopes=read_envelopes(memory, index.envelopes, variant))
    return rules


def read_rom_song(rom: RomImage, sound_id: int, index: SoundIndex | None = None,
                  fix_data_bugs: bool = True, variant: SmpsVariant | None = None) -> SmpsSong:
    """Sound `sound_id` as SmpsParser reads its asm (fix_data_bugs alike)."""
    return read_rom_code(rom, sound_id, index, fix_data_bugs, variant).song()
