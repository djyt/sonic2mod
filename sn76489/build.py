"""Auto-compile SN76489 (sn76489.c + panning.c) to a platform shared library.

The DLL is cached next to this file and only rebuilt when the C sources are newer.
Call ``get_lib_path()`` to obtain the compiled library path (building if needed).
"""

import os
import platform
import shutil
import subprocess
from pathlib import Path


# Paths relative to this file
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_SRC_DIR = _ROOT / "reference" / "SN76489"
_SRC_MAIN = _SRC_DIR / "sn76489.c"
_SRC_PAN  = _SRC_DIR / "panning.c"
_INC      = _SRC_DIR

_LIB_NAME = "sn76489.dll" if platform.system() == "Windows" else "sn76489.so"
_LIB_PATH = _HERE / _LIB_NAME


def _needs_rebuild() -> bool:
    """Return True if DLL is missing or older than the C sources."""
    if not _LIB_PATH.exists():
        return True
    lib_mtime = _LIB_PATH.stat().st_mtime
    return (_SRC_MAIN.stat().st_mtime > lib_mtime or
            _SRC_PAN.stat().st_mtime  > lib_mtime)


def _build_with_gcc(out: Path, inc: Path) -> None:
    cmd = [
        "gcc",
        "-shared",
        "-O2",
        f"-I{inc}",
        "-DVGM_LITTLE_ENDIAN",
        "-o", str(out),
        str(_SRC_MAIN),
        str(_SRC_PAN),
        "-lm",
    ]
    if platform.system() != "Windows":
        cmd.insert(1, "-fPIC")
    subprocess.run(cmd, check=True)


def _build_with_msvc(out: Path, inc: Path) -> None:
    obj = out.with_suffix(".obj")
    cmd = [
        "cl",
        "/LD",
        "/O2",
        f"/I{inc}",
        "/DVGM_LITTLE_ENDIAN",
        str(_SRC_MAIN),
        str(_SRC_PAN),
        f"/Fe:{out}",
        f"/Fo:{obj}",
        "/link",
    ]
    subprocess.run(cmd, check=True)
    for ext in (".exp", ".lib", ".obj"):
        p = out.with_suffix(ext)
        if p.exists():
            p.unlink()


def build() -> Path:
    """Compile sn76489.c + panning.c and return the path to the shared library."""
    if not _SRC_MAIN.exists():
        raise FileNotFoundError(
            f"SN76489 source not found: {_SRC_MAIN}\n"
            "Make sure reference/SN76489/sn76489.c is present."
        )

    print(f"Building {_LIB_NAME}...")

    if shutil.which("gcc"):
        _build_with_gcc(_LIB_PATH, _INC)
    elif shutil.which("cl"):
        _build_with_msvc(_LIB_PATH, _INC)
    else:
        raise RuntimeError(
            "No C compiler found on PATH.\n"
            "Install one of:\n"
            "  \u2022 GCC via MinGW-w64 / MSYS2 (recommended)\n"
            "  \u2022 MSVC via Visual Studio Build Tools\n"
            "Then re-run."
        )

    print(f"DLL built: {_LIB_PATH}")
    return _LIB_PATH


def get_lib_path() -> Path:
    """Return the compiled library path, building it first if necessary."""
    if _needs_rebuild():
        build()
    return _LIB_PATH
