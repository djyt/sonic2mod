"""Files parallel conversions share (the render cache, a minimal config's DAC samples): written
through a temp file beside them, then os.replace, so a reader in another process sees the old
file or the new one, never a part of one."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path


def write_atomic(path: Path, write: Callable[[Path], object]) -> None:
    """`write` fills a temp file, which then replaces `path`; on failure the temp file goes."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_shared(path: Path, data: bytes) -> None:
    """`data` to `path` unless it holds them already: the usual case, and on Windows a file another
    process has open cannot be replaced."""
    if path.is_file() and path.read_bytes() == data:
        return
    write_atomic(path, lambda tmp: tmp.write_bytes(data))
