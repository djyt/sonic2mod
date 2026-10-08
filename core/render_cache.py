"""A disk cache of chip renders (settings.yaml samples.render_cache).

A render is a pure function of its inputs: the chip is reset before each one.  So it is stored
under a hash of those inputs, beside the others the same code made, and a later conversion that
asks for the same render reads it back instead of running the emulator again:

    <dir>/<chip>/<salt>/<key[:2]>/<key>.bin      zlib(rate, typecode, samples)
    <dir>/<chip>/<salt>/<key[:2]>/<key><suffix>  a whole file (get_file / put_file: VGMPlay's WAVs;
                                                  get_bytes / put_bytes: a VGZ's frame log)
        salt  a hash of the emulator DLL and the Python a render runs through (code_salt)
        key   a hash of the render's inputs (RenderCache.key)

Only the chip render is cached; what follows it (shelf, DC block, loops, quantising) is cheap and
runs every time, so the settings that steer it are not part of the key.  Writes are atomic
(core/files.py): parallel conversions share one directory.  A file that cannot be read
is a miss; one that cannot be written is skipped.  Renders made by other code can never be read
again: the first cache opened with a new salt removes them.
"""

from __future__ import annotations

import array
import contextlib
import hashlib
import shutil
import struct
import threading
import zlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from .files import write_atomic

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

    def _path(self, key: str, suffix: str = ".bin") -> Path:
        assert self._dir is not None
        return self._dir / key[:2] / f"{key}{suffix}"

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

    def get_file(self, key: str, dest: Path, suffix: str) -> bool:
        """Copy the file stored under `key` to `dest`; False when there is none."""
        if self._dir is None:
            return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._path(key, suffix), dest)
        except OSError:
            self._count(hit=False)
            return False
        self._count(hit=True)
        return True

    def put_file(self, key: str, src: Path, suffix: str) -> None:
        """Store a copy of `src` under `key`."""
        self._store(key, suffix, lambda tmp: shutil.copyfile(src, tmp))

    def get_bytes(self, key: str, suffix: str) -> bytes | None:
        """The bytes stored under `key`, or None."""
        if self._dir is None:
            return None
        try:
            data = zlib.decompress(self._path(key, suffix).read_bytes())
        except (OSError, zlib.error):
            self._count(hit=False)
            return None
        self._count(hit=True)
        return data

    def put_bytes(self, key: str, data: bytes, suffix: str) -> None:
        """Store `data` under `key`."""
        self._store(key, suffix, lambda tmp: tmp.write_bytes(zlib.compress(data, 1)))

    def put(self, key: str, samples: Sequence, rate: int) -> None:
        """Store a render: an array as it is, a list as doubles ('d') or ints ('i') by its first value."""
        if not isinstance(samples, array.array):
            samples = array.array("d" if samples and isinstance(samples[0], float) else "i", samples)
        packed = _HEADER.pack(rate, samples.typecode.encode()) + samples.tobytes()
        self._store(key, ".bin", lambda tmp: tmp.write_bytes(zlib.compress(packed, 1)))

    def _store(self, key: str, suffix: str, write: Callable[[Path], object]) -> None:
        """Write a file under `key` atomically (core/files.py); a failure stores nothing."""
        if self._dir is None:
            return
        path = self._path(key, suffix)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, write)

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
