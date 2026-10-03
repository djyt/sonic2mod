"""A song or sound effect read from a ROM: header, track code and voices -> SmpsSong."""

from __future__ import annotations

from ..smps import SmpsSong, SongCode
from .detect import detect_driver
from .drivers import RomDriver
from .envelopes import read_envelopes
from .fixes import apply_fixes, data_fixes
from .header import read_music_header, read_sfx_header
from .image import RomImage
from .locate import SoundIndex, locate_sounds
from .tracks import decode_tracks
from .voices import read_voices, voices_used


def read_rom_code(rom: RomImage, sound_id: int, index: SoundIndex | None = None,
                  fix_data_bugs: bool = True, driver: RomDriver | None = None) -> SongCode:
    """Sound `sound_id` ($81 GHZ ... $93; SFX from $A0) before the walk.  `index`: the ROM's,
    when already located.  fix_data_bugs: the fixes known for this exact ROM (fixes.py), as the
    asm parser applies the disassembly's; False reads the game as shipped.  `driver`: detected
    when not given (detect.py)."""
    driver = driver or detect_driver(rom)
    index = index or locate_sounds(rom)
    address = index.address(sound_id)
    image, splices = apply_fixes(rom, data_fixes(rom) if fix_data_bugs else ())
    head = read_sfx_header(image, address) if index.is_sfx(sound_id) else read_music_header(image, address)

    tracks = decode_tracks(image, head.tracks, splices, driver)
    voices = read_voices(image, head.voices, voices_used(tracks.code)) if head.voices is not None else []
    if head.voices is not None:
        tracks.labels[head.header.voice_label] = head.voices
    envelopes = read_envelopes(image, index.envelopes, driver) if index.envelopes else None
    return SongCode(head.header, tracks.code, voices, address=address, addresses=tracks.labels,
                    driver=driver.name, dropped=dict(tracks.dropped), psg_envelopes=envelopes,
                    dac_names=driver.dac_names)


def read_rom_song(rom: RomImage, sound_id: int, index: SoundIndex | None = None,
                  fix_data_bugs: bool = True, driver: RomDriver | None = None) -> SmpsSong:
    """Sound `sound_id` as SmpsParser reads its asm (fix_data_bugs alike)."""
    return read_rom_code(rom, sound_id, index, fix_data_bugs, driver).song()
