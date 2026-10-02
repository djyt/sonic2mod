"""The MOD format: what a ProTracker file holds and how a sample plays in it.

    file.py          ModFile writer, read_mod reader, pattern breaks
    notes.py         PERIOD_TABLE, ModNote (C1..B3), MOD_NOTE_MAP
    volume.py        dB -> MOD volume (0..64)
    limits.py        bytes a sample may hold, the sustain that fits
    sample_audit.py  a written MOD's samples against the notes that play them
"""

from .file import (
    PAL_AMIGA_CLOCK,
    ModFile,
    ModImage,
    ModSample,
    apply_pattern_breaks,
    isolate_channel,
    read_mod,
    row_to_bcd,
    shift_for_breaks,
)
from .limits import MAX_MOD_SAMPLE_BYTES, max_sustain_secs, sample_limit_bytes
from .notes import MOD_NOTE_MAP, PERIOD_TABLE, ModNote
from .sample_audit import audit
from .volume import MOD_MAX_VOLUME, clamp_mod_volume, db_to_mod_volume, headroom_db

__all__ = [
    "MAX_MOD_SAMPLE_BYTES", "MOD_MAX_VOLUME", "MOD_NOTE_MAP", "PAL_AMIGA_CLOCK", "PERIOD_TABLE", "ModFile",
    "ModImage", "ModNote", "ModSample", "apply_pattern_breaks", "audit", "clamp_mod_volume", "db_to_mod_volume",
    "headroom_db", "isolate_channel", "max_sustain_secs", "read_mod", "row_to_bcd", "sample_limit_bytes",
    "shift_for_breaks"
]
