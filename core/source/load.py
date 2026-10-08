"""A song file -> SmpsSong, by its suffix: SMPS assembly is parsed, a ROM's bytecode decoded, a
VGM / VGZ rip lifted.

    .asm               ──SmpsParser───┐
    .bin / .md / .gen  ──read_rom_song┼──> SmpsSong ──> everything after the parser
    .vgm / .vgz        ──lift_song────┘
"""

from __future__ import annotations

from pathlib import Path

from ..rom import VARIANTS, DacSample, RomImage, dac_samples, is_rom_path, read_rom_song
from ..smps import DEFAULT_DRIVER, SmpsDriver, SmpsParser, SmpsSong
from ..vgm import LiftOptions, is_vgm_path, lift_song, load_frames


def read_song(path: str | Path, options: LiftOptions | None = None, rom_song: int | None = None,
              driver: SmpsDriver | None = None) -> SmpsSong:
    """The song in `path`; `options` say what a VGM log cannot (assembly states it all itself),
    `rom_song` which sound of a ROM ($81 ...), `driver` a ROM's variant (None: detected)."""
    if is_rom_path(path):
        if rom_song is None:
            raise ValueError(f"{path}: a ROM holds every song; rom_song: names which ($81 ...)")
        return read_rom_song(RomImage.load(path), rom_song, variant=VARIANTS[driver] if driver else None)

    if rom_song is not None:
        raise ValueError("rom_song: applies to a ROM input_file only (.bin / .md / .gen)")

    if is_vgm_path(path):
        return lift_song(load_frames(path), options)

    if options is not None and options.driver != DEFAULT_DRIVER:
        raise ValueError(f"driver: {options.driver}: the assembly parser reads {DEFAULT_DRIVER} songs only")
    return SmpsParser().parse_file(str(path))


def read_dac(path: str | Path) -> list[DacSample]:
    """A ROM's DAC samples (its driver's); nothing for any other input."""
    return dac_samples(RomImage.load(path)) if is_rom_path(path) else []
