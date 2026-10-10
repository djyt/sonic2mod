"""Auto-compile Nuked-OPN2 (3rdparty/nuked-opn2/ym3438.c + ym3438_batch.c) to a platform shared library.

The library is cached in build/ and only rebuilt when a C source is newer.
Call ``get_lib_path()`` to obtain the compiled library path (building if needed).

The compile itself lives in :mod:`core.chips.cbuild`, shared with core/chips/sn76489/build.py.
"""

from pathlib import Path

from ..cbuild import BUILD_DIR, THIRD_PARTY, CLibrary

_HERE = Path(__file__).parent
_INC = THIRD_PARTY / "nuked-opn2"

_LIB = CLibrary(
    name="ym3438",
    out_dir=BUILD_DIR,
    sources=[_INC / "ym3438.c", _HERE / "ym3438_batch.c"],
    include=_INC,
    missing_hint="Make sure 3rdparty/nuked-opn2/ym3438.c is present.",
)


def build() -> Path:
    """Compile ym3438.c and return the path to the shared library."""
    return _LIB.build()


def get_lib_path() -> Path:
    """Return the compiled library path, building it first if necessary."""
    return _LIB.get_lib_path()
