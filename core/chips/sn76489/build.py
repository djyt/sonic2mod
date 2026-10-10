"""Auto-compile SN76489 (3rdparty/sn76489/sn76489.c + panning.c) to a platform shared library.

The library is cached in build/ and only rebuilt when a C source is newer.
Call ``get_lib_path()`` to obtain the compiled library path (building if needed).

The compile itself lives in :mod:`core.chips.cbuild`, shared with core/chips/ym2612/build.py.
"""

from pathlib import Path

from ..cbuild import BUILD_DIR, THIRD_PARTY, CLibrary

_SRC_DIR = THIRD_PARTY / "sn76489"

_LIB = CLibrary(
    name="sn76489",
    out_dir=BUILD_DIR,
    sources=[_SRC_DIR / "sn76489.c", _SRC_DIR / "panning.c"],
    include=_SRC_DIR,
    defines=["VGM_LITTLE_ENDIAN"],
    gcc_libs=["-lm"],
    missing_hint="Make sure 3rdparty/sn76489/sn76489.c is present.",
)


def build() -> Path:
    """Compile sn76489.c + panning.c and return the path to the shared library."""
    return _LIB.build()


def get_lib_path() -> Path:
    """Return the compiled library path, building it first if necessary."""
    return _LIB.get_lib_path()
