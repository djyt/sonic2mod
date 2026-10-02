"""A disk cache of chip renders (settings.yaml samples.render_cache).

A render is a pure function of its inputs: the chip is reset before each one.  So it is stored
under a hash of those inputs, beside the others the same code made, and a later conversion that
asks for the same render reads it back instead of running the emulator again:

    <dir>/<chip>/<salt>/<key[:2]>/<key>.bin      zlib(rate, typecode, samples)
        salt  a hash of the emulator DLL and the Python a render runs through (code_salt)
        key   a hash of the render's inputs (RenderCache.key)

Only the chip render is cached; what follows it (shelf, DC block, loops, quantising) is cheap and
runs every time, so the settings that steer it are not part of the key.  Writes are atomic
(temp file + os.replace): parallel conversions share one directory.  A file that cannot be read
is a miss; one that cannot be written is skipped.  Renders made by other code can never be read
again: the first cache opened with a new salt removes them.
"""

from __future__ import annotations

import array
import hashlib
import os
import shutil
import struct
import threading
import zlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

# A file's header: playback rate, array typecode
_HEADER = struct.Struct("<Ic")

# The salt's characters a directory is named by
_SALT_CHARS = 16


def code_salt(paths: Iterable[Path]) -> str:
    """A hash of every file a render depends on: change one and every key changes."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


class RenderCache:
    """One chip's renders under `directory` (None: off - every get misses, put does nothing)."""

    def __init__(self, directory: str | Path | None, chip: str, salt: str):
        self._dir = Path(directory) / chip / salt[:_SALT_CHARS] if directory else None
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if self._dir is not None and not self._dir.exists():
            self._prune()

    def _prune(self) -> None:
        """Remove the renders other code made (the chip's other salt directories)."""
        assert self._dir is not None
        if not self._dir.parent.exists():
            return
        for old in self._dir.parent.iterdir():
            if old.is_dir() and old != self._dir:
                shutil.rmtree(old, ignore_errors=True)

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    def key(self, inputs: tuple) -> str:
        """The key of a render's inputs: a tuple of ints, floats, strings and tuples of them."""
        return hashlib.sha256(repr(inputs).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        assert self._dir is not None
        return self._dir / key[:2] / f"{key}.bin"

    def get(self, key: str) -> tuple[array.array, int] | None:
        """(samples, rate) stored under `key`, or None."""
        if self._dir is None:
            return None
        try:
            raw = zlib.decompress(self._path(key).read_bytes())
            rate, typecode = _HEADER.unpack_from(raw)
            samples = array.array(typecode.decode())
            samples.frombytes(raw[_HEADER.size:])
        except (OSError, zlib.error, struct.error, ValueError):
            self._count(hit=False)
            return None
        self._count(hit=True)
        return samples, rate

    def put(self, key: str, samples: Sequence, rate: int) -> None:
        """Store a render: an array as it is, a list as doubles ('d') or ints ('i') by its first value."""
        if self._dir is None:
            return
        if not isinstance(samples, array.array):
            samples = array.array("d" if samples and isinstance(samples[0], float) else "i", samples)
        path = self._path(key)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(zlib.compress(_HEADER.pack(rate, samples.typecode.encode()) + samples.tobytes(), 1))
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def through(self, inputs: tuple, render: Callable[[], tuple[Sequence, int]]) -> tuple[Sequence, int]:
        """render()'s (samples, rate), or what a render with the same inputs stored (an array)."""
        key = self.key(inputs)
        hit = self.get(key)
        if hit is not None:
            return hit
        samples, rate = render()
        self.put(key, samples, rate)
        return samples, rate

    def _count(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1
