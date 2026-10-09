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
    tracks     each kind of track's TrackRules: how the walk reads it where the driver differs
               from Sonic 1's (track(kind); a kind left out reads as Sonic 1's)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .driver_tables import PsgEnvelope

if TYPE_CHECKING:
    from .song import ChannelType


@dataclass(frozen=True)
class TrackRules:
    """What one kind of track (FM, PSG, the drum track) does that the walk resolves a driver's
    effects and ties by (core/smps/driver_track.py).  The defaults are Sonic 1's."""

    # VolumeStep: the level (FM: TL offset, PSG: attenuation) by step, a signed byte; none: no steps
    volume_steps: Mapping[int, int] = field(default_factory=dict)
    detune_shift: int = 0                   # the track adds its detune word >> this (the PSG's: a divider)
    jump_clears_tie: bool = False           # a jump drops a pending tie
    noise_writes_tone3: bool = True         # a noise note writes its pitch to tone 3; False: tone 3
                                            # keeps the last tone note's (none: divider 0, nMaxPSG)
    gate_spares_tied: bool = False          # the gate leaves a tied note whole (its key-off waits on the tie)
    gate_sees_tie: bool = False             # the gate leaves a note the next byte ties
    tied_rest_holds: int | None = None      # frames a rest after a tie holds the note; None: the whole rest
    rest_cuts: bool = False                 # the drum track's rest (and gate) stops the sample; False: it plays out


SONIC1_TRACK = TrackRules()


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
    tracks: Mapping[ChannelType, TrackRules] = field(default_factory=dict)

    def track(self, kind: ChannelType) -> TrackRules:
        """How a `kind` track reads; Sonic 1's where the driver states none."""
        return self.tracks.get(kind, SONIC1_TRACK)
