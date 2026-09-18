"""Auto-compile SN76489 (sn76489.c + panning.c) to a platform shared library.

The DLL is cached next to this file and only rebuilt when the C sources are newer.
Call ``get_lib_path()`` to obtain the compiled library path (building if needed).

The compile itself lives in :mod:`core.cbuild`, shared with ym2612/build.py.
"""

from pathlib import Path

from core.cbuild import CLibrary

_HERE = Path(__file__).parent
_SRC_DIR = _HERE.parent / "reference" / "SN76489"

_LIB = CLibrary(
    name="sn76489",
    out_dir=_HERE,
    sources=[_SRC_DIR / "sn76489.c", _SRC_DIR / "panning.c"],
    include=_SRC_DIR,
    defines=["VGM_LITTLE_ENDIAN"],
    gcc_libs=["-lm"],
    missing_hint="Make sure reference/SN76489/sn76489.c is present.",
)


def build() -> Path:
    """Compile sn76489.c + panning.c and return the path to the shared library."""
    return _LIB.build()


def get_lib_path() -> Path:
    """Return the compiled library path, building it first if necessary."""
    return _LIB.get_lib_path()
