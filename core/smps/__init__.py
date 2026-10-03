"""The source: Sonic 1 SMPS songs and the driver that plays them.

    song.py           SmpsSong and its parts (the parser's output)
    parser.py         SmpsParser: assembly -> SmpsSong
    song_prep.py      the song as the driver plays it (tempo-divider re-timing, short loops replayed)
    driver_tables.py  the driver's frequency tables, note indices, envelopes, operator order
    names.py          note labels, config pitch names, DAC names, SFX channel ids, source channel names
    track.py          TrackState: one track's driver state as its coordination flags leave it
    tempo.py          TempoSegment, tempo_schedule: the frame each tick is read on
    playback.py       played_song: what a song plays note by note, its spelling gone
    compare.py        compare_songs: where two songs play differently, aspect by aspect
"""

from .compare import ALL_ASPECTS, ChannelDiff, NoteDiff, SongDiff, align_songs, compare_songs
from .driver_tables import (
    DEFAULT_DRIVER,
    ENVELOPE_TERMINATOR,
    FM_FREQUENCIES,
    FM_SLOT_MASK,
    HW_FM_CHANNEL,
    PAN_VALUES,
    PSG_CHANNEL,
    PSG_ENVELOPES,
    PSG_ENVELOPES_BY_NAME,
    PSG_FREQUENCIES,
    PSG_FREQUENCIES_EXTENDED,
    SMPS_OP_TO_REG_OFFSET,
    SmpsDriver,
    chip_pitch,
    fm_note_index,
    noise_envelope_frames,
    psg_index_semitone,
    psg_note_index,
    psg_tone2_divider,
)
from .names import (
    flag_from_macro,
    flag_name,
    parse_smps_note,
    parse_synth_note,
    semitone_to_note_name,
    source_map,
    source_names,
    synth_note_name,
    voice_field_from_macro,
)
from .parser import SmpsParser
from .playback import Aspect, PlayedNote, PlayedSong, played_song
from .song import (
    CoordFlag,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsEffect,
    SmpsEvent,
    SmpsNote,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
    VoiceField,
    pan_is_hard,
    pan_side,
)
from .song_prep import apply_global_tempo_div, extend_looping_channels
from .tempo import TempoSegment, frame_of_tick, tempo_schedule, tick_at_frame
from .track import TrackState

__all__ = [
    "ALL_ASPECTS",
    "DEFAULT_DRIVER",
    "ENVELOPE_TERMINATOR",
    "FM_FREQUENCIES",
    "FM_SLOT_MASK",
    "HW_FM_CHANNEL",
    "PAN_VALUES",
    "PSG_CHANNEL",
    "PSG_ENVELOPES",
    "PSG_ENVELOPES_BY_NAME",
    "PSG_FREQUENCIES",
    "PSG_FREQUENCIES_EXTENDED",
    "SMPS_OP_TO_REG_OFFSET",
    "Aspect",
    "ChannelDiff",
    "CoordFlag",
    "NoteDiff",
    "PlayedNote",
    "PlayedSong",
    "SmpsChannel",
    "SmpsChannelHeader",
    "SmpsDriver",
    "SmpsEffect",
    "SmpsEvent",
    "SmpsNote",
    "SmpsParser",
    "SmpsSong",
    "SmpsSongHeader",
    "SmpsVoice",
    "SongDiff",
    "TempoSegment",
    "TrackState",
    "VoiceField",
    "align_songs",
    "apply_global_tempo_div",
    "chip_pitch",
    "compare_songs",
    "extend_looping_channels",
    "flag_from_macro",
    "flag_name",
    "fm_note_index",
    "frame_of_tick",
    "noise_envelope_frames",
    "pan_is_hard",
    "pan_side",
    "parse_smps_note",
    "parse_synth_note",
    "played_song",
    "psg_index_semitone",
    "psg_note_index",
    "psg_tone2_divider",
    "semitone_to_note_name",
    "source_map",
    "source_names",
    "synth_note_name",
    "tempo_schedule",
    "tick_at_frame",
    "voice_field_from_macro",
]
