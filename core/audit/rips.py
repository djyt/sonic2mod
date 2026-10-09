"""Which rip records which song: a config's VGM / VGZ, and a rip's config.

    configs/02_green_hill_zone.yaml             reference/vgz/02 - Green Hill Zone.vgz       by number
    configs/moonwalker/81_smooth_criminal.yaml  reference/vgz/moonwalker/03 - Smooth ...vgz  by rips.yaml

A set whose rips are numbered in another order than its configs (Moonwalker's: game order, the
configs sound-ID order) names each config's rip in a map beside the configs (RIPS_MAP: config
stem -> rip file name); without one the number prefix pairs them.  Given one folder, the other
mirrors it: configs/moonwalker <-> reference/vgz/moonwalker (RipShelf.around), unless the map
names the rips' folder under the rips' root (RIPS_FOLDER: Streets of Rage's `streets_of_rage_1`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import load_yaml
from ..vgm import is_vgm_path

RIPS_MAP = "rips.yaml"
RIPS_FOLDER = "folder"                      # a map's key, no config's stem: the rips' folder
_CONFIG_GLOB = "[0-9a-f][0-9a-f]_*.yaml"    # a song config: its sound's number first
_NUMBER = slice(0, 2)                       # "02 - Green Hill Zone.vgz" / "02_green_hill_zone.yaml" -> "02"


@dataclass(frozen=True)
class RipShelf:
    """A folder of song configs and the folder of their rips."""

    configs: Path
    rips: Path
    names: Mapping[str, str] | None = None      # config stem -> rip file name; None: by number

    @classmethod
    def load(cls, configs: str | Path, rips: str | Path, names: str | Path | None = None) -> RipShelf:
        """The shelf; `names` the map (default: RIPS_MAP beside the configs, when there is one)."""
        configs, rips = Path(configs), Path(rips)
        pairs = _read_map(configs, names)
        if pairs is None:
            return cls(configs, rips)
        return cls(configs, rips, {k: v for k, v in pairs.items() if k != RIPS_FOLDER})

    @classmethod
    def around(cls, configs: str | Path | None, rips: str | Path | None, names: str | Path | None = None, *,
               config_root: Path, rip_root: Path) -> RipShelf:
        """The shelf from the folders given, the one left out mirroring the other under its root
        (neither: the roots); ValueError when a folder given is not under its root."""
        configs = Path(configs) if configs else None
        rips = Path(rips) if rips else None
        if configs is None:
            configs = _mirror(rips, rip_root, config_root) if rips else config_root
        if rips is None:
            folder = (_read_map(configs, names) or {}).get(RIPS_FOLDER)
            rips = rip_root / folder if folder else _mirror(configs, config_root, rip_root)
        return cls.load(configs, rips, names)

    def rip_for(self, config: Path) -> Path | None:
        """The rip of `config`'s song, or None."""
        if self.names is not None:
            rip = self.rips / self.names[config.stem] if config.stem in self.names else None
            return rip if rip is not None and rip.exists() else None
        return next((p for p in self._rip_files() if p.name[_NUMBER] == config.stem[_NUMBER]), None)

    def config_for(self, rip: Path) -> Path | None:
        """The config whose song `rip` records, or None."""
        if self.names is not None:
            stem = next((s for s, name in self.names.items() if name == rip.name), None)
            config = None if stem is None else self.configs / f"{stem}.yaml"
            return config if config is not None and config.exists() else None
        return next((p for p in self.config_files() if p.stem[_NUMBER] == rip.name[_NUMBER]), None)

    def pairs(self) -> list[tuple[Path, Path]]:
        """(config, rip) of every rip whose config is on the shelf, in rip order."""
        found = ((self.config_for(rip), rip) for rip in self._rip_files())
        return [(config, rip) for config, rip in found if config is not None]

    def config_files(self) -> list[Path]:
        """The song configs; with a map, those it names."""
        configs = sorted(self.configs.glob(_CONFIG_GLOB))
        return configs if self.names is None else [c for c in configs if c.stem in self.names]

    def _rip_files(self) -> list[Path]:
        return sorted(p for p in self.rips.glob("*") if is_vgm_path(p))


def named(names: Iterable[str] | None, *paths: Path) -> bool:
    """A tool's --only: no names, or one a config's stem or a rip's name holds (02, green_hill)."""
    names = tuple(names or ())
    return not names or any(n in p.name for n in names for p in paths)


def _read_map(configs: Path, names: str | Path | None) -> dict[str, str] | None:
    """The map `names` (default: RIPS_MAP beside the configs), or None where there is none."""
    path = Path(names) if names else configs / RIPS_MAP
    if not path.exists():
        if names:
            raise FileNotFoundError(path)
        return None
    with path.open(encoding="utf-8") as f:
        pairs = load_yaml(f) or {}
    return {str(k): str(v) for k, v in pairs.items()}


def _mirror(path: Path, here: Path, there: Path) -> Path:
    """`path`'s folder under `there` as it sits under `here` (configs/moonwalker -> reference/vgz/moonwalker).
    Not resolve(): a linked folder keeps its place, on both sides."""
    folder, root = Path(os.path.abspath(path)), Path(os.path.abspath(here))
    if not folder.is_relative_to(root):
        raise ValueError(f"{path}: not under {here}; name its partner folder")
    return there / folder.relative_to(root)
