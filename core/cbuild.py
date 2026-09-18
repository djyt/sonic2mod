"""Compile a bundled C emulator to a platform shared library, with caching.

Both chip packages ship a C core from `reference/` and need the same thing: build
it on first use, rebuild it when the source changes, and work with either gcc or
MSVC.  Describe the library once with :class:`CLibrary` and call
:meth:`CLibrary.get_lib_path`.

    _LIB = CLibrary(
        name="ym3438",
        out_dir=Path(__file__).parent,
        sources=[_ROOT / "reference" / "Nuked-OPN2" / "ym3438.c"],
        include=_ROOT / "reference" / "Nuked-OPN2",
    )
    lib_path = _LIB.get_lib_path()
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_NO_COMPILER = (
    "No C compiler found on PATH.\n"
    "Install one of:\n"
    "  • GCC via MinGW-w64 / MSYS2 (recommended)\n"
    "  • MSVC via Visual Studio Build Tools\n"
    "Then re-run."
)


@dataclass
class CLibrary:
    """One compiled shared library: where its sources are and how to build it."""

    name: str                                   # library stem, e.g. "ym3438"
    out_dir: Path                               # where the .dll / .so is cached
    sources: list[Path]                         # C files to compile
    include: Path | None = None                 # -I / /I directory
    defines: list[str] = field(default_factory=list)      # e.g. ["VGM_LITTLE_ENDIAN"]
    gcc_libs: list[str] = field(default_factory=list)     # e.g. ["-lm"] (gcc only)
    missing_hint: str = ""                      # extra line for the not-found error

    @property
    def lib_name(self) -> str:
        return f"{self.name}.dll" if platform.system() == "Windows" else f"{self.name}.so"

    @property
    def lib_path(self) -> Path:
        return self.out_dir / self.lib_name

    def _needs_rebuild(self) -> bool:
        """True when the library is missing or older than any of its sources.

        A source that is not present (non-editable install) is not a reason to
        rebuild — the shipped library is used as-is.
        """
        if not self.lib_path.exists():
            return True
        if not all(src.exists() for src in self.sources):
            return False
        lib_mtime = self.lib_path.stat().st_mtime
        return any(src.stat().st_mtime > lib_mtime for src in self.sources)

    def _gcc_cmd(self) -> list[str]:
        cmd = ["gcc", "-shared", "-O2"]
        if platform.system() != "Windows":
            cmd.insert(1, "-fPIC")
        if self.include:
            cmd.append(f"-I{self.include}")
        cmd += [f"-D{d}" for d in self.defines]
        cmd += ["-o", str(self.lib_path)]
        cmd += [str(s) for s in self.sources]
        cmd += self.gcc_libs
        return cmd

    def _msvc_cmd(self) -> list[str]:
        cmd = ["cl", "/LD", "/O2"]
        if self.include:
            cmd.append(f"/I{self.include}")
        cmd += [f"/D{d}" for d in self.defines]
        cmd += [str(s) for s in self.sources]
        cmd += [f"/Fe:{self.lib_path}", f"/Fo:{self.lib_path.with_suffix('.obj')}", "/link"]
        return cmd

    def build(self) -> Path:
        """Compile the sources and return the shared library path."""
        missing = [src for src in self.sources if not src.exists()]
        if missing:
            hint = f"\n{self.missing_hint}" if self.missing_hint else ""
            raise FileNotFoundError(f"C source not found: {missing[0]}{hint}")

        print(f"Building {self.lib_name}...")
        if shutil.which("gcc"):
            subprocess.run(self._gcc_cmd(), check=True)
        elif shutil.which("cl"):
            subprocess.run(self._msvc_cmd(), check=True)
            # Remove MSVC side-effect files
            for ext in (".exp", ".lib", ".obj"):
                self.lib_path.with_suffix(ext).unlink(missing_ok=True)
        else:
            raise RuntimeError(_NO_COMPILER)

        print(f"DLL built: {self.lib_path}")
        return self.lib_path

    def get_lib_path(self) -> Path:
        """Return the compiled library path, building it first if necessary."""
        if self._needs_rebuild():
            self.build()
        return self.lib_path
