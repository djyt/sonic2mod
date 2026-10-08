"""Golden Axe: SMPS Z80 Type 0 FM (an early Type 1 FM).

    variant.py    the description: flags, the drum track
    layout.py     its header (track order, tempo 0) and 26-byte voice
    locate.py     the driver (its FM table) and the bank (its sound header) -> SoundIndex
    drums.py      the drum track's FM drum programs, run frame by frame
"""

from .variant import TYPE0FM

__all__ = ["TYPE0FM"]
