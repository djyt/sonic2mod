"""PlaybackRules: what a song's driver does that the song is played by - its tables, envelopes,
drum names and timing.  Each driver builds its own (core/drivers: Sonic 1's in
reference.py); a song and each of its channels carry them.  Nothing below the drivers
holds a driver's tables or falls back to one: whatever plays a note asks the song.

    tables     fm_frequencies   the FM words by fm_note_index (1 = nC0)
               psg_frequencies  the PSG dividers by psg_note_index (0 = nC0 = C3), as the driver stores them
               psg_read         the 128 words a 7-bit index reads: the table, then what lies past it
    envelopes  psg_envelopes    by smpsPSGvoice name (fTone_01 ...)
               adsr_presets     the ADSR envelopes a preset names (core/smps/adsr.py)
               pitch_envelopes  by SetPitchEnvelope's index (1 ...): a driver's frequency envelopes
    drums      dac_names        the drum track's bytes that play a sample, by name
               dac_pans         the B4 byte a sample writes to FM6 as it starts, by byte (OutRun's)
    timing     update_clock     what runs the update: a frame's length in seconds (core/smps/clock.py)
               tempo_phase      frames the first TempoWait hold comes late (core/smps/tempo.py)
               key_run_out      frames a note keys without an attacking read (core/smps/run_out.py)
    tracks     each kind of track's TrackRules: how the walk reads it where the driver differs
               from Sonic 1's (track(kind); a kind left out reads as Sonic 1's), by concern:
                   volume      the step a volume flag keeps, the level it sets, a register write over it
                   key_off     the gate and the rest: what they key off, a soft release instead
                   ties        what a jump and a tied read do to a tie
                   modulation  the vibrato's turns
                   pitch       the detune word's scale, the octave it is kept in, noise on tone 3
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING

from ..chips import PSG_DIVIDER_MASK, split_freq_word
from .adsr import Adsr
from .clock import VINT, UpdateClock
from .driver_tables import PsgEnvelope, PsgTable
from .pitch_envelope import PitchEnvelope

if TYPE_CHECKING:
    from .song import ChannelType


_BLOCK = 0x800                  # a frequency word's block, one step
_SEMITONES = 12
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
class VolumeRules:
    """The step a track's volume keeps and the level it sets."""

    # The level (FM: TL offset, PSG: attenuation) by the step the track keeps, a signed byte; none: no steps
    steps: Mapping[int, int] = field(default_factory=dict)
    # The step a VolumeStep's byte keeps; none: the byte (Streets of Rage 2's FM: v - 2, clamped 0-19)
    kept: Mapping[int, int] = field(default_factory=dict)
    step_range: range | None = None         # an AlterVolumeStep's sum clamped into it; None: it wraps (a signed byte)
    initial_step: int | None = None         # the step a track starts at, at its level (Streets of Rage 2's PSG:
                                            # 8, $40940); None: 0, no level until a volume flag
    tl_writes_hold: bool = False            # a register write to any TL holds until the next volume change or
                                            # voice: the volume write rewrites all four, a modulator's from the
                                            # voice (Streets of Rage 2's, $41A9A); False: a carrier's refused


@dataclass(frozen=True)
class SoftReleaseLaw:
    """A key-off that softens instead (Streets of Rage 2's `$FF $F3 n`, while on): where the gate or a
    rest would key a note off, it drops to the level of step (step + sign x n) >> 1 (a byte) and
    rings on through the rests, until the next note keys it off."""

    sign: int                               # FM +1 ($40EE0: half way to step n), PSG -1 ($42076: half of v - n)
    envelope_clears: bool = False           # a note under an envelope turns it off as it starts (the PSG's,
                                            # $420FE: its phase byte reset)
    voice_keys_off: bool = False            # a voice set keys the note off where it is on as the read ends
                                            # (FM's: the voice loads after the read, $410F4 / $416BA)

    def step(self, step: int, n: int) -> int:
        return ((step + self.sign * n) & 0xFF) >> 1


@dataclass(frozen=True)
class KeyOffRules:
    """What the gate and a rest key off."""

    gate_spares_tied: bool = False          # the gate leaves a tied note whole (its key-off waits on the tie)
    gate_sees_tie: bool = False             # the gate leaves a note the next byte ties
    tied_rest_holds: int | None = None      # frames a rest after a tie holds the note; None: the whole rest
    rest_cuts: bool = False                 # the drum track's rest (and gate) stops the sample; False: it plays out
    gate_floor: int | None = None           # frames a note no longer than the gate keys (Streets of Rage 2's
                                            # FM: 1, $40F72); None: it plays whole
    soft_release: SoftReleaseLaw | None = None   # the driver's soft release; None: it has none (refused)


@dataclass(frozen=True)
class TieRules:
    """What a jump and a tied read do to a tie."""

    jump_clears: bool = False               # a jump drops a pending tie
    exit_clears: bool = True                # a loop's exit drops a pending tie (Streets of Rage's $FE); False:
                                            # it keeps it (Streets of Rage 2's, $41E50)
    restarts_envelope: bool = True          # a tied read starts the PSG envelope again (Streets of Rage's
                                            # rips); False: it plays on from the attack (OutRun's, $0391)
    late_keys_off: bool = False             # a tie read after a flag, not straight after its note, comes after
                                            # the note's end keyed it off: the next note attacks (Streets of
                                            # Rage 2's, $41C9C); False: it ties
    drum_restarts: bool = True              # a tie on the drum track starts its sample again (Sonic 1's
                                            # DACUpdateTrack reads no tie); False: it plays on (Streets of
                                            # Rage 2's, $4277A)


@dataclass(frozen=True)
class ModulationRules:
    """How the modulation (vibrato) turns."""

    turn_pause: bool = True                 # the modulation spends a step at each turn: its counter's
                                            # reload adds nothing (Sonic 1); False: it moves too
    first_leg_short: int = 0                # the counter first loads half of (steps - this): Streets of
                                            # Rage's 1 (half its count, its reload the count + 1)


@dataclass(frozen=True)
class PitchRules:
    """How a note's frequency word is made from its table entry, detune and modulation."""

    word_shift: int = 0                     # the track adds its detune word, and its modulation's sum,
                                            # >> this (the PSG's: a divider)
    octave_shift_from: int | None = None    # ... >> the note's octave instead: (its table index + this) // 12
                                            # (Streets of Rage 2's PSG: 21, its table from octave 1's A)
    divider_clamp: bool = False             # the driver clamps the PSG divider to 1-1023 before it writes it
                                            # (Streets of Rage 2's, $4226C); False: the chip drops the bits above
    octave_wrap: OctaveWrap | None = None   # the word and its detune kept within an octave; None: added as is
    noise_writes_tone3: bool = True         # a noise note writes its pitch to tone 3; False: tone 3
                                            # keeps the last tone note's (none: divider 0, nMaxPSG)


    def divider(self, word: int) -> int:
        """The PSG divider the chip takes for a detuned `word`: 1-1023 where the driver clamps it, else
        its low 10 bits (a detune past them wraps: Streets of Rage's $8F PSG1 ramp, down to A2, then
        from the top again)."""
        if self.divider_clamp:
            return min(max(word, 1), PSG_DIVIDER_MASK)
        return word & PSG_DIVIDER_MASK

    def shift(self, index: int) -> int:
        """What the detune word and the modulation's sum are shifted by for the note at table `index`."""
        if self.octave_shift_from is None:
            return self.word_shift
        return (index + self.octave_shift_from) // _SEMITONES


@dataclass(frozen=True)
class TrackRules:
    """What one kind of track (FM, PSG, the drum track) does that the walk resolves a driver's
    effects and ties by (core/smps/driver_track.py), by concern.  The defaults are Sonic 1's."""

    volume: VolumeRules = VolumeRules()
    key_off: KeyOffRules = KeyOffRules()
    ties: TieRules = TieRules()
    modulation: ModulationRules = ModulationRules()
    pitch: PitchRules = PitchRules()
    byte_durations: bool = False            # a duration (byte x divider) is kept in a byte the track counts
                                            # up to: a product past 255 wraps, and 0 - a note before any
                                            # duration - lasts 256 ticks; False: the product as it is


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
    dac_pans: Mapping[int, int] = field(default_factory=dict)    # none: the track's pan alone
    adsr_presets: tuple[Adsr, ...] = ()           # the ADSR envelopes an EnvelopePreset names (Streets of Rage 2's)

    @cached_property
    def psg_table(self) -> PsgTable:
        """The PSG table a note's index reads: its words, and the pitch and length of its chromatic
        run, read from the driver's own table (a malformed one is a ValueError)."""
        return PsgTable.of(self.psg_frequencies, self.psg_read, self.driver)

    def track(self, kind: ChannelType) -> TrackRules:
        """How a `kind` track reads; Sonic 1's where the driver states none."""
        return self.tracks.get(kind, SONIC1_TRACK)
