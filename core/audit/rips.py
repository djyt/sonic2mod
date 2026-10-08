"""Which rip records which song: a config's VGM / VGZ, and a rip's config.

    configs/02_green_hill_zone.yaml             reference/vgz/02 - Green Hill Zone.vgz       by number
    configs/moonwalker/81_smooth_criminal.yaml  reference/vgz/moonwalker/03 - Smooth ...vgz  by rips.yaml

A set whose rips are numbered in another order than its configs (Moonwalker's: game order, the
configs sound-ID order) names each config's rip in a map beside the configs (RIPS_MAP: config
stem -> rip file name); without one the number prefix pairs them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import load_yaml

RIPS_MAP = "rips.yaml"
_CONFIG_GLOB = "[0-9a-f][0-9a-f]_*.yaml"    # a song config: its sound's number first
_NUMBER = slice(0, 2)                       # "02 - Green Hill Zone.vgz" / "02_green_hill_zone.yaml" -> "02"
_RIP_SUFFIXES = (".vgz", ".vgm")


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
        path = Path(names) if names else configs / RIPS_MAP
        if not path.exists():
            if names:
                raise FileNotFoundError(path)
            return cls(configs, rips)
        with path.open(encoding="utf-8") as f:
            pairs = load_yaml(f) or {}
        return cls(configs, rips, {str(k): str(v) for k, v in pairs.items()})

    def rip_for(self, config: Path) -> Path | None:
        """The rip of `config`'s song, or None."""
        if self.names is not None:
            return self.rips / self.names[config.stem] if config.stem in self.names else None
        return next((p for p in self._rip_files() if p.name[_NUMBER] == config.stem[_NUMBER]), None)

    def config_for(self, rip: Path) -> Path | None:
        """The config whose song `rip` records, or None."""
        if self.names is not None:
            stem = next((s for s, name in self.names.items() if name == rip.name), None)
            return None if stem is None else self.configs / f"{stem}.yaml"
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
        return sorted(p for p in self.rips.glob("*") if p.suffix.lower() in _RIP_SUFFIXES)
