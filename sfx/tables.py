"""Sonic 1 sound driver lookup tables — re-exported from :mod:`core.smps.driver_tables`.

The tables moved to ``core/`` so the converter can reach them without importing the
SFX driver: ``core`` must not depend on ``sfx``.  This module
stays as the name the SFX driver and its tests have always used.

New code should import from ``core.smps.driver_tables`` directly.
"""

from core.smps import (  # noqa: F401
    ENVELOPE_TERMINATOR,
    FM_FREQUENCIES,
    FM_SLOT_MASK,
    HW_FM_CHANNEL,
    PAN_VALUES,
    PSG_CHANNEL,
    PSG_ENVELOPES,
    PSG_FREQUENCIES,
    PSG_FREQUENCIES_EXTENDED,
    SMPS_OP_TO_REG_OFFSET,
    fm_note_index,
    psg_index_semitone,
    psg_note_index,
)
