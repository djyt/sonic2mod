"""Which rip records which song: a config's VGM / VGZ, and a rip's config.

    configs/sonic_1/02_green_hill_zone.yaml     reference/vgz/sonic_1/02 - Green Hill Zone.vgz  by number
    configs/moonwalker/81_smooth_criminal.yaml  reference/vgz/moonwalker/03 - Smooth ...vgz     by rips.yaml

A set whose rips are numbered in another order than its configs (Moonwalker's: game order, the
configs sound-ID order) names each config's rip in a map beside the configs (RIPS_MAP: config
stem -> rip file name); without one the number prefix pairs them.  Given one folder, the other
mirrors it: configs/moonwalker <-> reference/vgz/moonwalker (RipShelf.around), unless the map
names the rips' folder under the rips' root (RIPS_FOLDER: Streets of Rage's `streets_of_rage_1`).

A rip may record faults of its own, not its song's (RipFaults).  Its entry is then a mapping: the
rip's name under `rip:`, the faults beside it.  Frames are the rip's (core.vgm.FrameLog indices,
as `vgm_analyze.py --frames` and `vgm_frames.py --glitches` print them):

    81_harrier_saga: "02 - Harrier Saga (Stage Theme).vgz"       # a rip, no faults
    81_harrier_saga:
      rip: "02 - Harrier Saga (Stage Theme).vgz"
      glitches:                     # V-ints its driver lost (-1) or gained (+1): every channel shifts
        - {frame: 3080, frames: -1, why: "recorded in play: a V-int lost"}
      foreign:                      # another sound on these channels (from / to: frames; default: all)
        - {channels: [FM4], from: 120, to: 900, why: "a sound effect"}

The rip tools undo each glitch (core.vgm.realigned) and set foreign notes aside as known rip
faults, out of the verdicts.  Only what the evidence shows goes here: `vgm_frames.py --glitches`
prints candidates; a human confirms them.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_yaml
from ..vgm import FrameLog, is_vgm_path, realigned

_PROJECT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = _PROJECT / "configs"                  # the song configs, a folder per game below
RIP_ROOT = _PROJECT / "reference" / "vgz"           # their rips, mirrored
DEFAULT_SET = "sonic_1"                     # the folder pair a rip tool given neither reads: the reference driver's
RIPS_MAP = "rips.yaml"
RIPS_FOLDER = "folder"                      # a map's key, no config's stem: the rips' folder
_CONFIG_GLOB = "[0-9a-f][0-9a-f]_*.yaml"    # a song config: its sound's number first
_NUMBER = slice(0, 2)                       # "02 - Green Hill Zone.vgz" / "02_green_hill_zone.yaml" -> "02"

# A mapping entry's keys
_RIP, _GLITCHES, _FOREIGN = "rip", "glitches", "foreign"
_ENTRY_KEYS = frozenset({_RIP, _GLITCHES, _FOREIGN})
_GLITCH_KEYS = frozenset({"frame", "frames", "why"})
_FOREIGN_KEYS = frozenset({"channels", "from", "to", "why"})


@dataclass(frozen=True)
class RipGlitch:
    """A V-int the rip's driver lost or gained: every channel `frames` off its song from `frame` on."""

    frame: int                  # the rip's first frame off
    frames: int                 # -1: a V-int lost before it (the song a frame late), +1: one gained
    why: str


@dataclass(frozen=True)
class ForeignSound:
    """Another sound on some of the rip's channels (a sound effect, a voice left keyed): not the song's."""

    channels: tuple[str, ...]   # FM4, PSG1 ...
    why: str
    first: int | None = None    # the rip's frames it covers; None: from the start / to the end
    last: int | None = None


@dataclass(frozen=True)
class RipFaults:
    """What a rip records that its song does not play."""

    glitches: tuple[RipGlitch, ...] = ()
    foreign: tuple[ForeignSound, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.glitches or self.foreign)

    def realign(self, frames: FrameLog) -> FrameLog:
        """`frames` (the rip's) with every glitch undone: in step with its song throughout."""
        return realigned(frames, {g.frame: g.frames for g in self.glitches})

    def realigned_frame(self, frame: int) -> int:
        """Where the rip's `frame` sits in the realigned log."""
        return frame + sum(g.frames for g in self.glitches if g.frame <= frame)

    def foreign_at(self, channel: str, frame: int) -> bool:
        """Another sound holds `channel` on `frame` of the realigned log."""
        return any(channel in f.channels
                   and (f.first is None or frame >= self.realigned_frame(f.first))
                   and (f.last is None or frame <= self.realigned_frame(f.last)) for f in self.foreign)

    def foreign_throughout(self) -> set[str]:
        """The channels another sound holds for the whole rip."""
        return {ch for f in self.foreign if f.first is None and f.last is None for ch in f.channels}

    def lines(self) -> list[str]:
        """Each fault, a line: 'frame 3074 -1: burst at 3073 ... its V-int lost'."""
        out = [f"frame {g.frame} {g.frames:+d}: {g.why}" for g in self.glitches]
        for f in self.foreign:
            span = "" if f.first is None and f.last is None else f" frames {f.first or 0}-{'' if f.last is None else f.last}"
            out.append(f"{' '.join(f.channels)}{span}: another sound ({f.why})")
        return out


@dataclass(frozen=True)
class RipShelf:
    """A folder of song configs and the folder of their rips."""

    configs: Path
    rips: Path
    names: Mapping[str, str] | None = None      # config stem -> rip file name; None: by number
    faults: Mapping[str, RipFaults] = field(default_factory=dict)   # config stem -> its rip's faults

    @classmethod
    def load(cls, configs: str | Path, rips: str | Path, names: str | Path | None = None) -> RipShelf:
        """The shelf; `names` the map (default: RIPS_MAP beside the configs, when there is one)."""
        configs, rips = Path(configs), Path(rips)
        entries = _read_map(configs, names)
        if entries is None:
            return cls(configs, rips)
        songs = {stem: entry for stem, entry in entries.items() if stem != RIPS_FOLDER}
        return cls(configs, rips, {stem: rip for stem, (rip, _) in songs.items()},
                   {stem: faults for stem, (_, faults) in songs.items() if faults})

    @classmethod
    def around(cls, configs: str | Path | None, rips: str | Path | None, names: str | Path | None = None, *,
               config_root: Path = CONFIG_ROOT, rip_root: Path = RIP_ROOT) -> RipShelf:
        """The shelf from the folders given, the one left out mirroring the other under its root
        (neither: DEFAULT_SET's); ValueError when a folder given is not under its root."""
        configs = Path(configs) if configs else None
        rips = Path(rips) if rips else None
        if configs is None:
            configs = _mirror(rips, rip_root, config_root) if rips else config_root / DEFAULT_SET
        if rips is None:
            folder = (_read_map(configs, names) or {}).get(RIPS_FOLDER)
            rips = rip_root / folder[0] if folder else _mirror(configs, config_root, rip_root)
        return cls.load(configs, rips, names)

    def rip_for(self, config: Path) -> Path | None:
        """The rip of `config`'s song, or None."""
        if self.names is not None:
            rip = self.rips / self.names[config.stem] if config.stem in self.names else None
            return rip if rip is not None and rip.exists() else None
        return next((p for p in self._rip_files() if p.name[_NUMBER] == config.stem[_NUMBER]), None)

    def faults_for(self, config: Path) -> RipFaults:
        """What `config`'s rip records that its song does not play (none: an empty RipFaults)."""
        return self.faults.get(config.stem, RipFaults())

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


def _read_map(configs: Path, names: str | Path | None) -> dict[str, tuple[str, RipFaults]] | None:
    """The map `names` (default: RIPS_MAP beside the configs): each key's rip (or folder) and the
    rip's faults; None where there is no map.  ValueError on an entry it cannot read."""
    path = Path(names) if names else configs / RIPS_MAP
    if not path.exists():
        if names:
            raise FileNotFoundError(path)
        return None
    with path.open(encoding="utf-8") as f:
        entries = load_yaml(f) or {}
    try:
        return {str(k): _entry(v) for k, v in entries.items()}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from e


def _entry(value: object) -> tuple[str, RipFaults]:
    """A map entry: a rip's name, or {rip:, glitches:, foreign:}."""
    if not isinstance(value, dict):
        return str(value), RipFaults()
    _check_keys(value, _ENTRY_KEYS)

    glitches = []
    for g in value.get(_GLITCHES) or ():
        _check_keys(g, _GLITCH_KEYS)
        if not int(g["frames"]):
            raise ValueError(f"glitch at frame {g['frame']}: 0 frames")
        glitches.append(RipGlitch(int(g["frame"]), int(g["frames"]), str(g["why"])))

    foreign = []
    for f in value.get(_FOREIGN) or ():
        _check_keys(f, _FOREIGN_KEYS)
        first, last = f.get("from"), f.get("to")
        foreign.append(ForeignSound(tuple(str(c) for c in f["channels"]), str(f["why"]),
                                    None if first is None else int(first), None if last is None else int(last)))
    return str(value[_RIP]), RipFaults(tuple(glitches), tuple(foreign))


def _check_keys(entry: object, keys: frozenset[str]) -> None:
    """ValueError unless `entry` is a mapping of `keys` only."""
    if not isinstance(entry, dict):
        raise ValueError(f"{entry!r}: not a mapping")
    unknown = {str(k) for k in entry} - keys
    if unknown:
        raise ValueError(f"unknown key{'s' * (len(unknown) > 1)} {', '.join(sorted(unknown))}")


def _mirror(path: Path, here: Path, there: Path) -> Path:
    """`path`'s folder under `there` as it sits under `here` (configs/moonwalker -> reference/vgz/moonwalker).
    Not resolve(): a linked folder keeps its place, on both sides."""
    folder, root = Path(os.path.abspath(path)), Path(os.path.abspath(here))
    if not folder.is_relative_to(root):
        raise ValueError(f"{path}: not under {here}; name its partner folder")
    return there / folder.relative_to(root)
