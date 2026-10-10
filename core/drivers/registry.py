"""Every driver by name, each loaded on first use: a song reads with its driver alone, so a run
imports one driver however many there are.  Detection (detect.py) loads them all.

    name (names.py)  ->  (package under core/drivers, the SmpsVariant it builds)
"""

from __future__ import annotations

import importlib
from functools import cache

from core.rom.variant import SmpsVariant

from .names import SmpsDriver

_PACKAGES: dict[SmpsDriver, tuple[str, str]] = {
    SmpsDriver.SONIC1: ("smps68k.sonic1", "SONIC1"),
    SmpsDriver.TYPE1A: ("smps68k.type1a", "TYPE1A"),
    SmpsDriver.TYPE0FM: ("smpsz80.type0fm", "TYPE0FM"),
    SmpsDriver.MUCOM: ("smps68k.mucom", "MUCOM"),
}

assert set(_PACKAGES) == set(SmpsDriver), "a driver name without a package"


@cache
def load_driver(name: SmpsDriver) -> SmpsVariant:
    package, attribute = _PACKAGES[name]
    driver = getattr(importlib.import_module(f"{__package__}.{package}"), attribute)
    assert driver.name == name, f"{package}.{attribute} is {driver.name}, not {name}"
    return driver


def all_drivers() -> tuple[SmpsVariant, ...]:
    """Every driver, in name order (names.py)."""
    return tuple(load_driver(name) for name in SmpsDriver)
