"""A register log lifted back into the song the driver played: VgmLog -> SmpsSong.

The SMPS driver is deterministic, so each write pattern maps back to the flag that produced it
(frequency table -> note byte, TempoWait hold frames -> ticks, PSG envelope curves ->
smpsPSGvoice, ...).  Everything after the parser runs unchanged on the result.
Plan and status: docs/todo/vgz_conversion.md, Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import DEFAULT_DRIVER, SmpsDriver, SmpsSong
from .reader import VgmError, VgmLog


class VgmLiftError(VgmError):
    """The log is not one the driver could have written, or the lift cannot read it yet."""


@dataclass(frozen=True)
class LiftOptions:
    """What the log cannot say for itself (a config's driver: / tempo_modifier: / tempo_divider:)."""

    driver: SmpsDriver = DEFAULT_DRIVER
    tempo_modifier: int | None = None     # None: inferred from the frames TempoWait holds
    tempo_divider: int | None = None      # None: inferred from the note durations


def lift_song(log: VgmLog, options: LiftOptions | None = None) -> SmpsSong:
    """The song `log` is a recording of, as SmpsParser would have read it from the asm."""
    options = options or LiftOptions()
    raise VgmLiftError(f"lifting a VGM log ({options.driver} driver) is not implemented yet: "
                       "docs/todo/vgz_conversion.md Phase 1")
