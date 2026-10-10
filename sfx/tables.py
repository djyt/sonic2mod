"""Sonic 1 sound driver lookup tables — re-exported: Sonic 1's own from
:mod:`core.drivers.reference`, the SMPS-wide ones from :mod:`core.smps.driver_tables`.

``core`` must not depend on ``sfx``.  This module stays as the name the SFX driver (Sonic 1's) and
its tests have always used.
"""

from core.drivers.reference import (  # noqa: F401
    ENVELOPE_TERMINATOR,
    FM_FREQUENCIES,
    PSG_ENVELOPES,
    PSG_ENVELOPES_BY_NAME,
    PSG_FREQUENCIES,
    PSG_FREQUENCIES_EXTENDED,
    SONIC1_RULES,
)
from core.smps import (  # noqa: F401
    FM_SLOT_MASK,
    HW_FM_CHANNEL,
    PAN_VALUES,
    PSG_CHANNEL,
    SMPS_OP_TO_REG_OFFSET,
    fm_note_index,
    psg_note_index,
)
