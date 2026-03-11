"""Auto-compile Nuked-OPN2 (ym3438.c) to a platform shared library.

The DLL is cached next to this file and only rebuilt when the C source is newer.
Call ``get_lib_path()`` to obtain the compiled library path (building if needed).
"""

import platform
import shutil
import subprocess
from pathlib import Path

# Paths relative to this file
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_SRC  = _ROOT / "reference" / "Nuked-OPN2" / "ym3438.c"
_INC  = _ROOT / "reference" / "Nuked-OPN2"

_LIB_NAME = "ym3438.dll" if platform.system() == "Windows" else "ym3438.so"
_LIB_PATH = _HERE / _LIB_NAME


def _needs_rebuild() -> bool:
    """Return True if DLL is missing or older than the C source."""
    if not _LIB_PATH.exists():
        return True
    return _SRC.stat().st_mtime > _LIB_PATH.stat().st_mtime


def _build_with_gcc(src: Path, out: Path, inc: Path) -> None:
    cmd = [
        "gcc",
        "-shared",
        "-O2",
        f"-I{inc}",
        "-o", str(out),
        str(src),
    ]
    if platform.system() != "Windows":
        cmd.insert(1, "-fPIC")
    subprocess.run(cmd, check=True)


def _build_with_msvc(src: Path, out: Path, inc: Path) -> None:
    # cl.exe needs the output in the same dir; use a temp obj file
    obj = out.with_suffix(".obj")
    cmd = [
        "cl",
        "/LD",          # create DLL
        "/O2",
        f"/I{inc}",
        str(src),
        f"/Fe:{out}",
        f"/Fo:{obj}",
        "/link",
    ]
    subprocess.run(cmd, check=True)
    # Remove MSVC side-effect files
    for ext in (".exp", ".lib", ".obj"):
        p = out.with_suffix(ext)
        if p.exists():
            p.unlink()


def build() -> Path:
    """Compile ym3438.c and return the path to the shared library."""
    if not _SRC.exists():
        raise FileNotFoundError(
            f"Nuked-OPN2 source not found: {_SRC}\n"
            "Make sure reference/Nuked-OPN2/ym3438.c is present."
        )

    print(f"Building {_LIB_NAME}...")

    if shutil.which("gcc"):
        _build_with_gcc(_SRC, _LIB_PATH, _INC)
    elif shutil.which("cl"):
        _build_with_msvc(_SRC, _LIB_PATH, _INC)
    else:
        raise RuntimeError(
            "No C compiler found on PATH.\n"
            "Install one of:\n"
            "  • GCC via MinGW-w64 / MSYS2 (recommended)\n"
            "  • MSVC via Visual Studio Build Tools\n"
            "Then re-run."
        )

    print(f"DLL built: {_LIB_PATH}")
    return _LIB_PATH


def get_lib_path() -> Path:
    """Return the compiled library path, building it first if necessary."""
    if _needs_rebuild():
        build()
    return _LIB_PATH
