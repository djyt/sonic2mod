"""The MOD format: what a ProTracker file holds and how a sample plays in it.

    file.py          ModFile writer, read_mod reader, read_sample (a sample_list file), pattern breaks
    notes.py         PERIOD_TABLE, note_rate / period_rate, ModNote (C1..B3), MOD_NOTE_MAP
    volume.py        dB -> MOD volume (0..64)
    limits.py        bytes a sample may hold, the sustain that fits
    reach.py         how far into each sample a written MOD is heard (ProTracker's replay, tick by tick)
    sample_audit.py  a written MOD's samples against the notes that play them
    timing.py        when each row of one pass plays (Fxx followed), EDx delays
"""

from .file import (
    PAL_AMIGA_CLOCK,
    ROWS_PER_PATTERN,
    ModFile,
    ModImage,
    ModSample,
    apply_pattern_breaks,
    isolate_channel,
    read_mod,
    read_sample,
    row_to_bcd,
    shift_for_breaks,
)
from .limits import MAX_MOD_SAMPLE_BYTES, max_sustain_secs, sample_limit_bytes
from .notes import MOD_NOTE_MAP, PERIOD_TABLE, ModNote, note_rate, period_rate
from .reach import UnmodelledEffect, heard_bytes
from .sample_audit import LOW_RATE_HZ, audit
from .timing import DEFAULT_BPM, DEFAULT_SPEED, TICK_SECS_AT_1_BPM, TimedRow, edx_delay, timed_pass
from .volume import MOD_MAX_VOLUME, clamp_mod_volume, db_to_mod_volume, headroom_db

__all__ = [
    "DEFAULT_BPM",
    "DEFAULT_SPEED",
    "LOW_RATE_HZ",
    "MAX_MOD_SAMPLE_BYTES",
    "MOD_MAX_VOLUME",
    "MOD_NOTE_MAP",
    "PAL_AMIGA_CLOCK",
    "PERIOD_TABLE",
    "ROWS_PER_PATTERN",
    "TICK_SECS_AT_1_BPM",
    "ModFile",
    "ModImage",
    "ModNote",
    "ModSample",
    "TimedRow",
    "UnmodelledEffect",
    "apply_pattern_breaks",
    "audit",
    "clamp_mod_volume",
    "db_to_mod_volume",
    "edx_delay",
    "headroom_db",
    "heard_bytes",
    "isolate_channel",
    "max_sustain_secs",
    "note_rate",
    "period_rate",
    "read_mod",
    "read_sample",
    "row_to_bcd",
    "sample_limit_bytes",
    "shift_for_breaks",
    "timed_pass"
]
