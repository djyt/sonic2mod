"""Auto-compile Nuked-OPN2 (ym3438.c + ym3438_batch.c) to a platform shared library.

The DLL is cached next to this file and only rebuilt when the C source is newer.
Call ``get_lib_path()`` to obtain the compiled library path (building if needed).

The compile itself lives in :mod:`core.cbuild`, shared with sn76489/build.py.
"""

from pathlib import Path

from core.cbuild import CLibrary

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_INC = _ROOT / "reference" / "Nuked-OPN2"

_LIB = CLibrary(
    name="ym3438",
    out_dir=_HERE,
    sources=[_INC / "ym3438.c", _HERE / "ym3438_batch.c"],
    include=_INC,
    missing_hint="Make sure reference/Nuked-OPN2/ym3438.c is present.",
)


def build() -> Path:
    """Compile ym3438.c and return the path to the shared library."""
    return _LIB.build()


def get_lib_path() -> Path:
    """Return the compiled library path, building it first if necessary."""
    return _LIB.get_lib_path()
