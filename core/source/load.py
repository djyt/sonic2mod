"""A song file -> SmpsSong, by its suffix: SMPS assembly is parsed, a VGM / VGZ rip is lifted.

    .asm          ──SmpsParser───┐
                                 ├──> SmpsSong ──> everything after the parser
    .vgm / .vgz   ──lift_song────┘
"""

from __future__ import annotations

from pathlib import Path

from ..smps import DEFAULT_DRIVER, SmpsParser, SmpsSong
from ..vgm import LiftOptions, is_vgm_path, lift_song, load_frames


def read_song(path: str | Path, options: LiftOptions | None = None) -> SmpsSong:
    """The song in `path`; `options` say what a VGM log cannot (assembly states it all itself)."""
    if is_vgm_path(path):
        return lift_song(load_frames(path), options)

    if options is not None and options.driver != DEFAULT_DRIVER:
        raise ValueError(f"driver: {options.driver}: the assembly parser reads {DEFAULT_DRIVER} songs only")
    return SmpsParser().parse_file(str(path))
