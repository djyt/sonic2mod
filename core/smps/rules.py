"""PlaybackRules: what a song's driver does that the song is played by - its tables, envelopes,
drum names and timing.  Each driver builds its own (core/drivers: Sonic 1's in
reference.py); a song and each of its channels carry them.  Nothing below the drivers
holds a driver's tables or falls back to one: whatever plays a note asks the song.

    tables     fm_frequencies   the FM words by fm_note_index (1 = nC0)
               psg_frequencies  the PSG dividers by psg_note_index (0 = nC0 = C3), as the driver stores them
               psg_read         the 128 words a 7-bit index reads: the table, then what lies past it
    envelopes  psg_envelopes    by smpsPSGvoice name (fTone_01 ...)
    drums      dac_names        the drum track's bytes that play a sample, by name
    timing     tempo_phase      frames the first TempoWait hold comes late (core/smps/tempo.py)
               key_run_out      frames a note keys without an attacking read (core/smps/run_out.py)
    the walk   volume_steps     each kind's level (FM: TL offset, PSG: attenuation) by VolumeStep,
                                the step a signed byte
               psg_detune_shift the PSG adds the detune word >> this to its divider
               jump_clears_tie  the kinds of track a jump drops a pending tie on
               noise_writes_tone3  a noise note writes its pitch to tone 3; False: tone 3 keeps the
                                last tone note's (none: divider 0, nMaxPSG)
               gate_spares_tied the kinds whose gate leaves a tied note whole (the key-off waits on the tie)
               gate_sees_tie    the kinds whose gate leaves a note the next byte ties
               tied_rest_holds  each kind's frames a rest after a tie holds the note before the key-off;
                                a kind left out holds it through the rest (Sonic 1)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .driver_tables import PsgEnvelope

if TYPE_CHECKING:
    from .song import ChannelType


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
    volume_steps: Mapping[ChannelType, Mapping[int, int]] = field(default_factory=dict)
    psg_detune_shift: int = 0
    jump_clears_tie: frozenset[ChannelType] = frozenset()
    noise_writes_tone3: bool = True
    gate_spares_tied: frozenset[ChannelType] = frozenset()
    gate_sees_tie: frozenset[ChannelType] = frozenset()
    tied_rest_holds: Mapping[ChannelType, int] = field(default_factory=dict)
