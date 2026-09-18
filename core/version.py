"""The project version, from pyproject.toml — the single place it is written.

`importlib.metadata` only knows the version the package had when it was last installed, so a
checkout that has moved on prints a stale number.  Read pyproject.toml next to the package first;
fall back to the installed metadata (a wheel install has no pyproject.toml), then to "dev".
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def get_version() -> str:
    try:
        with PYPROJECT.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return importlib.metadata.version("sonic2mod")
    except importlib.metadata.PackageNotFoundError:
        return "dev"
