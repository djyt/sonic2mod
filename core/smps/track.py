"""One track's driver state (SMPS_Track) as its coordination flags leave it: what a note plays with.

    header ──► TrackState.for_header ──apply(flag)──► ... ──► the state a note reads

Config-free: the conversion's DriverState (core/plan) adds the MOD routing on top, and the song
walk that compares a parse with a lift (playback.py) reads it as it is.
"""

from __future__ import annotations

from ..chips import FM_TL_SILENT, PSG_ATT_SILENT, fm_level_db, psg_level_db
from .song import ChannelType, CoordFlag, SmpsChannelHeader, SmpsEffect, pan_side


class TrackState:
    """Mutable track state, advanced one coordination flag at a time.  Unknown flags are ignored."""

    __slots__ = ("att", "detune", "envelope", "fill", "hard_panned", "is_psg", "modulation",
                 "modulation_on", "noise_form", "pan", "tl", "transpose", "voice")

    def __init__(self, *, is_psg: bool, transpose: int = 0, volume: int = 0) -> None:
        self.is_psg = is_psg
        self.transpose = transpose          # header pitch_offset + every smpsChangeTransposition
        self.tl = 0 if is_psg else volume   # YM2612 TL offset, 0-127
        self.att = volume if is_psg else 0  # SN76489 attenuation, 0-15
        self.hard_panned = False
        self.pan = "C"                      # smpsPan: "L", "R" or "C"
        self.detune = 0                     # smpsDetune / smpsAlterNote: raw FNUM (PSG: divider) offset
        self.voice: int | None = None       # smpsSetvoice index
        self.envelope: str | None = None    # the driver's VoiceIndex: header voice, then every smpsPSGvoice
        self.noise_form: int | None = None  # the smpsPSGform byte once one ran (SMPS_Track.PSGNoise); permanent
        self.fill = 0                       # smpsNoteFill frames, 0 = off
        self.modulation: tuple[int, ...] | None = None   # smpsModSet (wait, speed, delta, steps)
        self.modulation_on = False          # smpsModSet / smpsModOn on, smpsModOff off

    @classmethod
    def for_header(cls, header: SmpsChannelHeader) -> TrackState:
        """A track as its header starts it: transpose, volume and PSG voice."""
        st = cls(is_psg=header.channel_type == ChannelType.PSG, transpose=header.pitch_offset, volume=header.volume)
        st.envelope = header.psg_voice_label or None
        return st

    def apply(self, effect: SmpsEffect) -> None:
        """Advance the state past one coordination flag."""
        kind = effect.flag

        if kind == CoordFlag.SET_VOICE:
            self.voice = effect.params[0]

        elif kind == CoordFlag.ALTER_VOL:
            delta = effect.params[0]
            if self.is_psg:
                self.att = max(0, min(PSG_ATT_SILENT, self.att + delta))
            else:
                self.tl = max(0, min(FM_TL_SILENT, self.tl + delta))

        elif kind == CoordFlag.SET_VOL:
            if self.is_psg:
                self.att = max(0, min(PSG_ATT_SILENT, effect.params[0]))
            else:
                self.tl = max(0, min(FM_TL_SILENT, effect.params[0]))

        elif kind == CoordFlag.PAN:
            self.pan = pan_side(effect.params)
            self.hard_panned = self.pan != "C"

        elif kind == CoordFlag.DETUNE:
            # SMPS_Track.Detune: added to the frequency word the driver writes (about 10 cents
            # per unit on FM).  Not a semitone: it never moves a note or a range lookup; it is
            # what a chorus pair's beating and a composite layer's FNUM offset come from.
            self.detune = effect.params[0]

        elif kind == CoordFlag.CHANGE_TRANSPOSITION:
            self.transpose += effect.params[0]

        elif kind == CoordFlag.PSG_FORM:
            # cfSetPSGNoise: a noise channel from here on (nothing in Sonic 1 music turns it back)
            self.noise_form = effect.params[0]

        elif kind == CoordFlag.PSG_VOICE:
            # cfSetPSGTone: VoiceIndex, in tone and noise mode alike
            self.envelope = effect.params[0]

        elif kind == CoordFlag.NOTE_FILL:
            self.fill = effect.params[0]

        elif kind == CoordFlag.MOD_SET:
            self.modulation = tuple(effect.params)
            self.modulation_on = True

        elif kind in (CoordFlag.MOD_ON, CoordFlag.MOD_OFF):
            self.modulation_on = kind == CoordFlag.MOD_ON

    @property
    def in_noise_mode(self) -> bool:
        """True once smpsPSGform ran on this channel; nothing in Sonic 1 music leaves it."""
        return self.noise_form is not None

    def level_db(self, pan_law_db: float) -> float:
        """Hardware level of a note played right now, relative to full scale."""
        if self.is_psg:
            return psg_level_db(self.att)
        return fm_level_db(self.tl, self.hard_panned, pan_law_db)

    @property
    def is_silent(self) -> bool:
        """True when the track's own volume puts a note below audibility."""
        return self.att >= PSG_ATT_SILENT if self.is_psg else self.tl >= FM_TL_SILENT
