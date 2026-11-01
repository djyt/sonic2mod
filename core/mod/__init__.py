"""The MOD format: what a ProTracker file holds and how a sample plays in it.

    cell.py          one pattern cell's 4 bytes (Cell), ROWS_PER_PATTERN
    file.py          ModFile writer, read_sample (a sample_list file)
    reader.py        read_mod: a MOD read back (ModImage), isolate_channel
    breaks.py        pattern breaks: a pattern ended early, the rows after it repacked
    notes.py         PERIOD_TABLE, note_rate / period_rate, ModNote (C1..B3), MOD_NOTE_MAP
    volume.py        dB -> MOD volume (0..64)
    limits.py        bytes a sample may hold, the sustain that fits
    reach.py         how far into each sample a written MOD is heard (ProTracker's replay, tick by tick)
    sample_audit.py  a written MOD's samples against the notes that play them
    timing.py        when each row of one pass plays (Fxx followed), EDx delays
"""

from .breaks import apply_pattern_breaks, row_to_bcd, shift_for_breaks
from .cell import ROWS_PER_PATTERN
from .file import MAX_INSTRUMENT, PAL_AMIGA_CLOCK, ModFile, ModSample, read_sample
from .limits import MAX_MOD_SAMPLE_BYTES, max_sustain_secs, sample_limit_bytes
from .notes import (
    FINETUNE_STEPS,
    MOD_NOTE_MAP,
    MOD_NOTES,
    PERIOD_TABLE,
    ModNote,
    clamp_note,
    finetune_ratio,
    note_rate,
    period_rate,
    shift_ratio,
    shifted_rate,
)
from .reach import UnmodelledEffect, heard_bytes
from .reader import SAMPLE_NAME_BYTES, ModImage, isolate_channel, read_mod
from .sample_audit import LOW_RATE_HZ, audit
from .timing import DEFAULT_BPM, DEFAULT_SPEED, TICK_SECS_AT_1_BPM, TimedRow, edx_delay, timed_pass
from .volume import MOD_MAX_VOLUME, clamp_mod_volume, db_to_mod_volume, headroom_db

__all__ = [
    "DEFAULT_BPM",
    "DEFAULT_SPEED",
    "FINETUNE_STEPS",
    "LOW_RATE_HZ",
    "MAX_INSTRUMENT",
    "MAX_MOD_SAMPLE_BYTES",
    "MOD_MAX_VOLUME",
    "MOD_NOTES",
    "MOD_NOTE_MAP",
    "PAL_AMIGA_CLOCK",
    "PERIOD_TABLE",
    "ROWS_PER_PATTERN",
    "SAMPLE_NAME_BYTES",
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
    "clamp_note",
    "db_to_mod_volume",
    "edx_delay",
    "finetune_ratio",
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
    "shift_ratio",
    "shifted_rate",
    "timed_pass"
]
