"""PlaybackRules: what a song's driver does that the song is played by - its tables, envelopes,
drum names and timing.  Each driver builds its own (core/drivers: Sonic 1's in
smps68k/sonic1/tables.py); a song and each of its channels carry them.  Nothing below the drivers
holds a driver's tables or falls back to one: whatever plays a note asks the song.

    tables     fm_frequencies   the FM words by fm_note_index (1 = nC0)
               psg_frequencies  the PSG dividers by psg_note_index (0 = nC0 = C3), as the driver stores them
               psg_read         the 128 words a 7-bit index reads: the table, then what lies past it
    envelopes  psg_envelopes    by smpsPSGvoice name (fTone_01 ...)
    drums      dac_names        the drum track's bytes that play a sample, by name
    timing     tempo_phase      frames the first TempoWait hold comes late (core/smps/tempo.py)
               key_run_out      frames a note keys without an attacking read (core/smps/run_out.py)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .driver_tables import PsgEnvelope


@dataclass(frozen=True)
class PlaybackRules:
    driver: str                                   # its name, for messages
    fm_frequencies: tuple[int, ...]
    psg_frequencies: tuple[int, ...]
    psg_read: tuple[int, ...]
    psg_envelopes: Mapping[str, PsgEnvelope]
    dac_names: Mapping[int, str]
    tempo_phase: int = 0
    key_run_out: int | None = None                # None: never
