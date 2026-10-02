"""The source: Sonic 1 SMPS songs and the driver that plays them.

    song.py           SmpsSong and its parts (the parser's output)
    parser.py         SmpsParser: assembly -> SmpsSong
    song_prep.py      the song as the driver plays it (tempo-divider re-timing, short loops replayed)
    driver_tables.py  the driver's frequency tables, note indices, envelopes, operator order
    levels.py         the chip level laws (TL 0.75 dB/step, attenuation 2 dB/step, pan law)
    names.py          note labels, config pitch names, DAC names, SFX channel ids, source channel names
"""

from .driver_tables import (
    CARRIER_OFFSETS_BY_ALG,
    ENVELOPE_TERMINATOR,
    FM_FREQUENCIES,
    FM_SAMPLE_RATE,
    FM_SLOT_MASK,
    HW_FM_CHANNEL,
    MD_FM_CLOCK,
    MD_PSG_CLOCK,
    PAN_VALUES,
    PSG_CHANNEL,
    PSG_ENVELOPES,
    PSG_ENVELOPES_BY_NAME,
    PSG_FREQUENCIES,
    PSG_FREQUENCIES_EXTENDED,
    PSG_SAMPLE_RATE,
    SMPS_OP_TO_REG_OFFSET,
    carrier_names,
    chip_pitch,
    fm_note_index,
    noise_envelope_frames,
    psg_index_semitone,
    psg_note_index,
    psg_tone2_divider,
)
from .levels import (
    DEFAULT_FM_PAN_LAW_DB,
    FM_TL_SILENT,
    PSG_ATT_SILENT,
    PSG_STEP_DB,
    TL_STEP_DB,
    fm_level_db,
    psg_level_db,
)
from .names import parse_smps_note, parse_synth_note, semitone_to_note_name, source_map, source_names, synth_note_name
from .parser import SmpsParser
from .song import SmpsChannel, SmpsEvent, SmpsNote, SmpsSong, SmpsSongHeader, SmpsVoice, pan_is_hard, pan_side
from .song_prep import apply_global_tempo_div, extend_looping_channels

__all__ = [
    "CARRIER_OFFSETS_BY_ALG", "DEFAULT_FM_PAN_LAW_DB", "ENVELOPE_TERMINATOR", "FM_FREQUENCIES", "FM_SAMPLE_RATE",
    "FM_SLOT_MASK", "FM_TL_SILENT", "HW_FM_CHANNEL", "MD_FM_CLOCK", "MD_PSG_CLOCK", "PAN_VALUES", "PSG_ATT_SILENT", "PSG_CHANNEL", "PSG_ENVELOPES",
    "PSG_ENVELOPES_BY_NAME", "PSG_FREQUENCIES", "PSG_FREQUENCIES_EXTENDED", "PSG_SAMPLE_RATE", "PSG_STEP_DB",
    "SMPS_OP_TO_REG_OFFSET", "TL_STEP_DB", "SmpsChannel", "SmpsEvent", "SmpsNote", "SmpsParser", "SmpsSong",
    "SmpsSongHeader", "SmpsVoice", "apply_global_tempo_div", "carrier_names", "chip_pitch",
    "extend_looping_channels", "fm_level_db", "fm_note_index", "noise_envelope_frames", "pan_is_hard", "pan_side",
    "parse_smps_note", "parse_synth_note", "psg_index_semitone", "psg_level_db", "psg_note_index",
    "psg_tone2_divider", "semitone_to_note_name", "source_map", "source_names", "synth_note_name"
]
