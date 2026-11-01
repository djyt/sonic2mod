"""A rip's frame log kept on disk: decoding and replaying a VGZ is most of a lift's time (about a
second for Green Hill), and the lift is run again and again on the same rips.

    <dir>/vgm_frames/<salt>/<key[:2]>/<key>.frames     zlib(pickle(FrameLog))
        salt  a hash of the code a frame log is made by (reader, chipstate, frames, core/chips)
        key   a hash of the file's bytes (and the cut, the DAC samples given)

The directory is settings.yaml samples.render_cache (core/render_cache.py); None reads the rip.
"""

from __future__ import annotations

import hashlib
import pickle
from collections.abc import Mapping
from functools import cache
from pathlib import Path

from ..render_cache import RenderCache, code_salt
from .frames import FrameCut, FrameLog, frame_log
from .reader import decode_vgm

_KIND = "vgm_frames"
_SUFFIX = ".frames"
_HERE = Path(__file__).resolve().parent
_CODE = [_HERE / "reader.py", _HERE / "chipstate.py", _HERE / "frames.py", *(_HERE.parent / "chips").glob("*.py")]


@cache
def _salt() -> str:
    return code_salt(_CODE)


def load_frames(path: str | Path, cache_dir: str | Path | None = None, cut: FrameCut = FrameCut.VINT,
                dac_samples: Mapping[int, bytes] | None = None) -> FrameLog:
    """The frame log of the VGM / VGZ at `path`, cut as its driver's update runs (its DAC starts
    found by the driver's `dac_samples`: frame_log), from `cache_dir` when it holds it."""
    raw = Path(path).read_bytes()
    store = RenderCache(cache_dir, _KIND, _salt() if cache_dir else "")
    parts = [hashlib.sha256(raw).hexdigest()]
    if cut is not FrameCut.VINT:
        parts.append(cut.value)
    if dac_samples:
        played = hashlib.sha256()
        for sound, data in sorted(dac_samples.items()):
            played.update(sound.to_bytes(2) + len(data).to_bytes(4) + data)
        parts.append(played.hexdigest())
    key = store.key(tuple(parts))

    blob = store.get_bytes(key, _SUFFIX)
    if blob is not None:
        return pickle.loads(blob)

    frames = frame_log(decode_vgm(raw), cut=cut, dac_samples=dac_samples)
    store.put_bytes(key, pickle.dumps(frames, pickle.HIGHEST_PROTOCOL), _SUFFIX)
    return frames
