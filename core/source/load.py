"""A song file -> SmpsSong, by its suffix: SMPS assembly is parsed, a ROM's bytecode decoded, a
VGM / VGZ rip lifted.

    .asm               ──SmpsParser───┐
    .bin / .md / .gen  ──read_rom_song┼──> SmpsSong ──> everything after the parser
    .vgm / .vgz        ──lift_song────┘
"""

from __future__ import annotations

from pathlib import Path

from ..drivers import VARIANTS, dac_samples, read_rom_song
from ..drivers.names import DEFAULT_DRIVER, SmpsDriver
from ..rom import DacSample, RomImage, SmpsVariant, is_rom_path
from ..smps import SmpsParser, SmpsSong
from ..vgm import LiftOptions, is_vgm_path, lift_song, load_frames


def read_song(path: str | Path, options: LiftOptions | None = None, rom_song: int | None = None,
              driver: SmpsDriver | None = None, fix_data_bugs: bool = True) -> SmpsSong:
    """The song in `path`; `options` say what a VGM log cannot (assembly states it all itself),
    `rom_song` which sound of a ROM ($81 ...), `driver` a ROM's variant (None: detected; an asm or
    a rip: Sonic 1's, the only driver either is read by).  `fix_data_bugs` False: an asm or a ROM
    as the game shipped (what a rip recorded)."""
    if is_rom_path(path):
        if rom_song is None:
            raise ValueError(f"{path}: a ROM holds every song; rom_song: names which ($81 ...)")
        return read_rom_song(RomImage.load(path), rom_song, fix_data_bugs=fix_data_bugs, variant=_variant(driver))

    if rom_song is not None:
        raise ValueError("rom_song: applies to a ROM input_file only (.bin / .md / .gen)")

    if driver not in (None, DEFAULT_DRIVER):
        raise ValueError(f"driver: {driver}: an asm or a rip is read as {DEFAULT_DRIVER}'s only")
    rules = VARIANTS[DEFAULT_DRIVER].rules
    if is_vgm_path(path):
        return lift_song(load_frames(path), rules, options)
    return SmpsParser(rules, fix_data_bugs=fix_data_bugs).parse_file(str(path))


def read_dac(path: str | Path, driver: SmpsDriver | None = None) -> list[DacSample]:
    """A ROM's DAC samples (its driver's: `driver`, else detected); nothing for any other input."""
    if not is_rom_path(path):
        return []
    return dac_samples(RomImage.load(path), _variant(driver))


def _variant(driver: SmpsDriver | None) -> SmpsVariant | None:
    """The variant a config's driver: names; None: the ROM's own (detected)."""
    return VARIANTS[driver] if driver else None
