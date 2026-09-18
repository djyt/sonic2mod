"""Sonic 1 sound driver lookup tables — re-exported from :mod:`core.driver_tables`.

The tables moved to ``core/`` so the converter can reach them without importing the
SFX driver: ``core`` is the bottom layer and must not depend on ``sfx``.  This module
stays as the name the SFX driver and its tests have always used.

New code should import from ``core.driver_tables`` directly.
"""

from core.driver_tables import (  # noqa: F401
    CARRIER_OFFSETS_BY_ALG,
    ENVELOPE_TERMINATOR,
    FM_FREQUENCIES,
    FM_SAMPLE_RATE,
    FM_SLOT_MASK,
    HW_FM_CHANNEL,
    PAN_VALUES,
    PSG_CHANNEL,
    PSG_ENVELOPES,
    PSG_FREQUENCIES,
    PSG_FREQUENCIES_EXTENDED,
    PSG_SAMPLE_RATE,
    SMPS_OP_TO_REG_OFFSET,
    fm_note_index,
    psg_index_semitone,
    psg_note_index,
)
