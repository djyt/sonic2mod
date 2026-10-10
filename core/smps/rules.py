"""PlaybackRules: what a song's driver does that the song is played by - its tables, envelopes,
drum names and timing.  Each driver builds its own (core/drivers: Sonic 1's in
reference.py); a song and each of its channels carry them.  Nothing below the drivers
holds a driver's tables or falls back to one: whatever plays a note asks the song.

    tables     fm_frequencies   the FM words by fm_note_index (1 = nC0)
               psg_frequencies  the PSG dividers by psg_note_index (0 = nC0 = C3), as the driver stores them
               psg_read         the 128 words a 7-bit index reads: the table, then what lies past it
    envelopes  psg_envelopes    by smpsPSGvoice name (fTone_01 ...)
               pitch_envelopes  by SetPitchEnvelope's index (1 ...): a driver's frequency envelopes
    drums      dac_names        the drum track's bytes that play a sample, by name
    timing     update_clock     what runs the update: a frame's length in seconds (core/smps/clock.py)
               tempo_phase      frames the first TempoWait hold comes late (core/smps/tempo.py)
               key_run_out      frames a note keys without an attacking read (core/smps/run_out.py)
    tracks     each kind of track's TrackRules: how the walk reads it where the driver differs
               from Sonic 1's (track(kind); a kind left out reads as Sonic 1's)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..chips import split_freq_word
from .clock import VINT, UpdateClock
from .driver_tables import PsgEnvelope
from .pitch_envelope import PitchEnvelope

if TYPE_CHECKING:
    from .song import ChannelType


_BLOCK = 0x800                  # a frequency word's block, one step
_WORD_MASK = 0xFFFF


@dataclass(frozen=True)
class OctaveWrap:
    """A driver keeping an FM track's fnum within an octave as it adds the detune: at or under `low`
    the word drops a block and the fnum gains `drop`, over `high` it rises one and loses `rise`.

        OutRun (Z80 $0563)              $283  $508   +$285  -$284
        Golden Axe's drum slides ($0262)  $27E  $4FE   +$280  -$280
    """

    low: int
    high: int
    drop: int
    rise: int

    def wrapped(self, word: int) -> int:
        fnum, _ = split_freq_word(word)
        if fnum <= self.low:
            return (word - _BLOCK + self.drop) & _WORD_MASK
        if fnum > self.high:
            return (word + _BLOCK - self.rise) & _WORD_MASK
        return word


@dataclass(frozen=True)
class TrackRules:
    """What one kind of track (FM, PSG, the drum track) does that the walk resolves a driver's
    effects and ties by (core/smps/driver_track.py).  The defaults are Sonic 1's."""

    # VolumeStep: the level (FM: TL offset, PSG: attenuation) by step, a signed byte; none: no steps
    volume_steps: Mapping[int, int] = field(default_factory=dict)
    word_shift: int = 0                     # the track adds its detune word, and its modulation's sum,
                                            # >> this (the PSG's: a divider)
    modulation_turn_pause: bool = True      # the modulation spends a step at each turn: its counter's
                                            # reload adds nothing (Sonic 1); False: it moves too
    jump_clears_tie: bool = False           # a jump drops a pending tie
    noise_writes_tone3: bool = True         # a noise note writes its pitch to tone 3; False: tone 3
                                            # keeps the last tone note's (none: divider 0, nMaxPSG)
    gate_spares_tied: bool = False          # the gate leaves a tied note whole (its key-off waits on the tie)
    gate_sees_tie: bool = False             # the gate leaves a note the next byte ties
    tied_rest_holds: int | None = None      # frames a rest after a tie holds the note; None: the whole rest
    rest_cuts: bool = False                 # the drum track's rest (and gate) stops the sample; False: it plays out
    byte_durations: bool = False            # a duration (byte x divider) is kept in a byte the track counts
                                            # up to: a product past 255 wraps, and 0 - a note before any
                                            # duration - lasts 256 ticks; False: the product as it is
    octave_wrap: OctaveWrap | None = None   # the word and its detune kept within an octave; None: added as is
    tie_restarts_envelope: bool = True      # a tied read starts the PSG envelope again (Streets of Rage's
                                            # rips); False: it plays on from the attack (OutRun's, $0391)


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
    pitch_envelopes: Mapping[int, PitchEnvelope] = field(default_factory=dict)
    update_clock: UpdateClock = VINT

    def track(self, kind: ChannelType) -> TrackRules:
        """How a `kind` track reads; Sonic 1's where the driver states none."""
        return self.tracks.get(kind, SONIC1_TRACK)
