"""Tick-accurate offline reimplementation of the Sonic 1 SMPS SFX driver.

One `tick()` call is one V-int.  SFX always run one tick per V-int — `TempoWait`
only walks the *music* track RAM to apply its compensation, so the music
`fps * (modifier-1)/modifier` correction does not apply here (:186).

Routine names and line references map onto `sonic_1/s1.sounddriver.asm`.

The driver's `addq.w #4,sp` "skip the caller's return" trick is expressed here as
a boolean return value: `_do_modulation` returns True only when it produced a new
frequency, which is the only case where the caller writes the frequency register.
"""

from __future__ import annotations

from .chips import (
    fm_key_off,
    fm_key_on,
    fm_send_tl,
    fm_send_voice,
    fm_set_freq,
    fm_set_pan,
    psg_note_off,
    psg_set_freq,
    psg_set_noise,
    psg_set_volume,
)
from .tables import (
    ENVELOPE_TERMINATOR,
    FM_FREQUENCIES,
    PAN_VALUES,
    PSG_ENVELOPES,
    PSG_FREQUENCIES,
    PSG_FREQUENCIES_EXTENDED,
    fm_note_index,
    psg_note_index,
)
from .track import SfxTrack


def _s8(value: int) -> int:
    """Interpret an unsigned byte as signed (ext.b -> ext.w)."""
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


class SfxDriver:
    """Plays one parsed SFX into a YM2612 and an SN76489."""

    def __init__(self, song, opn2, sn, psg_oob: str = "extend"):
        """
        Args:
            song:     SmpsSong parsed from an SFX .asm (header.is_sfx must be True).
            opn2:     ym2612.wrapper.OPN2 instance, already reset.
            sn:       sn76489.wrapper.SN76489 instance, already reset.
            psg_oob:  "extend" or "clamp" — how to treat PSG note indices past the
                      end of the 70-entry driver table.
        """
        self.song = song
        self.opn2 = opn2
        self.sn = sn
        self.psg_table = PSG_FREQUENCIES_EXTENDED if psg_oob == "extend" else PSG_FREQUENCIES
        self.psg_clamp = psg_oob != "extend"
        self.warnings: list[str] = []

        self.tracks = [
            SfxTrack.from_header(ch.header, ch.events, name=ch.header.label)
            for ch in song.channels
        ]

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def tick(self) -> bool:
        """Advance every track by one V-int.  Returns True while any track plays."""
        for track in self.tracks:
            if not track.playing:
                continue
            if track.is_fm:
                self._fm_update(track)
            else:
                self._psg_update(track)
        return any(t.playing for t in self.tracks)

    # ------------------------------------------------------------------
    # FM  — FMUpdateTrack :343
    # ------------------------------------------------------------------

    def _fm_update(self, t: SfxTrack) -> None:
        t.duration_timeout -= 1
        if t.duration_timeout != 0:
            self._note_timeout_update(t)
            if not t.playing:
                return
            if self._do_modulation(t):
                fm_set_freq(self.opn2, t, t.freq + t.mod_val + t.detune)
            return

        t.no_attack = False
        if not self._do_next(t):
            return

        # FMPrepareNote :531 — resting tracks write nothing; Freq == 0 means rest.
        if not t.at_rest:
            if t.freq == 0:
                t.at_rest = True
            else:
                fm_set_freq(self.opn2, t, t.freq + t.detune)
        if not t.at_rest:
            fm_key_on(self.opn2, t)

    # ------------------------------------------------------------------
    # PSG — PSGUpdateTrack :1843
    # ------------------------------------------------------------------

    def _psg_update(self, t: SfxTrack) -> None:
        t.duration_timeout -= 1
        if t.duration_timeout != 0:
            self._note_timeout_update(t)
            if not t.playing:
                return
            if t.voice_index:                       # PSGUpdateVolFX gates on this
                self._psg_do_vol_fx(t)
            if self._do_modulation(t):
                psg_set_freq(self.sn, t, t.freq + t.mod_val + t.detune)
            return

        t.no_attack = False
        if not self._do_next(t):
            return

        # PSGDoNoteOn :1949 — a negative Freq marks the rest sentinel.
        if t.freq >= 0 and not t.at_rest:
            psg_set_freq(self.sn, t, t.freq + t.detune)
        self._psg_do_vol_fx(t)                      # PSGDoVolFX — NOT gated on voice_index

    # ------------------------------------------------------------------
    # Shared event walk — FMDoNext :362 / PSGDoNext :1862
    # ------------------------------------------------------------------

    def _do_next(self, t: SfxTrack) -> bool:
        """Consume coord flags then one note.  False if the track stopped."""
        t.at_rest = False

        # Coord flags are handled BEFORE the key-off: the driver's .noteloop runs
        # CoordFlag until it reads a byte below $E0, and only then reaches .gotnote.
        while t.event_index < len(t.events):
            event = t.events[t.event_index]
            if event.effect is None:
                break
            self._coord_flag(t, event.effect)
            t.event_index += 1
            if not t.playing:
                return False
        else:
            self._stop_track(t)
            return False

        note = t.events[t.event_index].note
        t.event_index += 1
        t.no_attack = note.is_no_attack

        # .gotnote: FMNoteOff, suppressed when the no-attack bit is set.  PSGDoNext
        # has no equivalent — a PSG track only keys off via a rest, a note-fill
        # expiry, or smpsStop.
        if t.is_fm and not t.no_attack:
            fm_key_off(self.opn2, t)
        # A standalone duration byte takes the .gotduration path, which never calls
        # FMSetFreq/PSGSetFreq — the channel re-keys at whatever frequency it already
        # holds.  Re-deriving it here would differ whenever a smpsChangeTransposition
        # landed in between (SndA8 - SS Goal).
        if not note.is_retrigger:
            if note.is_rest and not note.is_no_attack:
                self._set_rest(t)
            elif not note.is_rest:
                self._set_freq_from_note(t, note.note_value)

        self._set_duration(t, note.duration)
        self._finish_track_update(t)
        return True

    def _set_freq_from_note(self, t: SfxTrack, note_value: int) -> None:
        if t.is_fm:
            index = fm_note_index(note_value, t.transpose)
            if index >= len(FM_FREQUENCIES):
                # andi.w #$7F permits 96..127, which reads past the 96-entry table
                # into the routines that follow it in ROM.  SndA8 - SS Goal gets
                # there legitimately: 38 cumulative smpsAlterPitch steps take its
                # index to 98.  We cannot reproduce the opcode bytes, and the FM
                # block field saturates at 7 anyway, so drop whole octaves until
                # the note is representable.  That keeps the pitch class and just
                # caps the octave, which is what drivers that compute the octave
                # instead of using a table do.
                self._warn(t, f"FM note index {index} past end of table — octave capped")
                index = 84 + (index - 96) % 12
            t.freq = FM_FREQUENCIES[index]
        else:
            index = psg_note_index(note_value, t.transpose)
            if index >= len(PSG_FREQUENCIES):
                if self.psg_clamp or index >= len(self.psg_table):
                    self._warn(t, f"PSG note index {index} past end of table — clamped")
                    index = len(PSG_FREQUENCIES) - 1
                else:
                    self._warn(t, f"PSG note index {index} past end of table — extrapolated")
            t.freq = self.psg_table[index]

    def _set_rest(self, t: SfxTrack) -> None:
        """TrackSetRest :430 / PSGSetFreq .restpsg :1903."""
        t.at_rest = True
        if t.is_fm:
            t.freq = 0
        else:
            t.freq = -1
            psg_note_off(self.sn, t)

    def _set_duration(self, t: SfxTrack, duration: int) -> None:
        """SetDuration :406 — the parser has already applied the tempo divider."""
        t.saved_duration = duration & 0xFF
        t.duration_timeout = t.saved_duration

    def _finish_track_update(self, t: SfxTrack) -> None:
        """FinishTrackUpdate :437.

        The whole re-arm is skipped when the no-attack bit is set — that is what
        lets a tied note carry its PSG envelope and modulation sweep forward.
        """
        if t.no_attack:
            return
        t.vol_env_index = 0
        if t.mod_active:
            wait, speed, delta, steps = t.mod_data
            t.mod_wait = wait
            t.mod_speed = speed
            t.mod_delta = delta
            t.mod_steps = steps >> 1        # lsr.b #1 — halved on every note re-arm
            t.mod_val = 0

    def _note_timeout_update(self, t: SfxTrack) -> None:
        """NoteTimeoutUpdate :463 (smpsNoteFill).  No Sonic 1 SFX uses it."""
        if t.note_timeout == 0:
            return
        t.note_timeout -= 1
        if t.note_timeout != 0:
            return
        t.at_rest = True
        if t.is_fm:
            fm_key_off(self.opn2, t)
        else:
            psg_note_off(self.sn, t)

    # ------------------------------------------------------------------
    # Modulation — DoModulation :488
    # ------------------------------------------------------------------

    def _do_modulation(self, t: SfxTrack) -> bool:
        """True only when a step was produced, i.e. when the caller must rewrite freq."""
        if not t.mod_active:
            return False
        if t.mod_wait:
            t.mod_wait -= 1
            return False
        t.mod_speed -= 1
        if t.mod_speed:
            return False
        t.mod_speed = t.mod_data[1]
        if t.mod_steps == 0:
            # Direction change.  Note the reload is the RAW step byte, NOT halved —
            # only the initial arm halves it, so the first half-swing is short and
            # every later swing is full length.
            t.mod_steps = t.mod_data[3]
            t.mod_delta = (-t.mod_delta) & 0xFF
            return False
        t.mod_steps -= 1
        t.mod_val += _s8(t.mod_delta)
        return True

    # ------------------------------------------------------------------
    # PSG volume envelope — PSGDoVolFX :1966
    # ------------------------------------------------------------------

    def _psg_do_vol_fx(self, t: SfxTrack) -> None:
        d6 = t.volume & 0xFF
        if t.voice_index:
            table = PSG_ENVELOPES[t.voice_index - 1]
            index = min(t.vol_env_index, len(table) - 1)
            value = table[index]
            t.vol_env_index = index + 1
            if value == ENVELOPE_TERMINATOR:
                # VolEnvHold :2021 — rewind one so the last real value repeats
                # forever, and write no volume at all this tick.
                t.vol_env_index -= 1
                return
            d6 = (d6 + value) & 0xFF
        if d6 >= 0x10:                  # cmpi.b #$10 / blo — else forced to silence
            d6 = 0x0F
        if t.at_rest:
            return
        # A no-attack track still writes volume here: PSGCheckNoteTimeout falls
        # through to PSGSendVolume whenever NoteTimeoutMaster is 0, which it always
        # is for Sonic 1 SFX (none use smpsNoteFill).
        psg_set_volume(self.sn, t, d6)

    # ------------------------------------------------------------------
    # Coordination flags
    # ------------------------------------------------------------------

    def _coord_flag(self, t: SfxTrack, effect) -> None:
        kind = effect.effect_type
        params = effect.params

        if kind == 'smpsSetvoice':
            t.voice_index = params[0]
            voice = self._voice(t, params[0])
            if voice is not None:
                fm_send_voice(self.opn2, t, voice)

        elif kind == 'smpsAlterVol':
            t.volume = (t.volume + params[0]) & 0xFF
            if t.is_fm:
                fm_send_tl(self.opn2, t)

        elif kind == 'smpsAlterNote':
            t.detune = params[0]                    # cfDetune :2180 — SET, not added

        elif kind == 'smpsChangeTransposition':
            t.transpose = (t.transpose + params[0]) & 0xFF

        elif kind == 'smpsModSet':
            wait, speed, delta, steps = params
            t.mod_active = True
            t.mod_data = (wait, speed, delta, steps)
            t.mod_wait = wait
            t.mod_speed = speed
            t.mod_delta = delta
            t.mod_steps = steps >> 1
            t.mod_val = 0

        elif kind == 'smpsModOn':
            t.mod_active = True

        elif kind == 'smpsModOff':
            t.mod_active = False

        elif kind == 'smpsNoteFill':
            t.note_timeout_master = params[0]
            t.note_timeout = params[0]

        elif kind == 'smpsPan':
            self._set_pan(t, params[0])

        elif kind == 'smpsPSGform':
            t.voice_control = 0xE0
            psg_set_noise(self.sn, params[0])

        elif kind == 'smpsPSGvoice':
            t.voice_index = self._psg_voice_index(t, params[0])

        elif kind in ('smpsNop', 'smpsChanTempoDiv'):
            pass

        else:
            self._warn(t, f"unhandled coord flag {kind}")

    def _set_pan(self, t: SfxTrack, raw: str) -> None:
        """smpsPan operand is the raw macro text, e.g. 'panRight, $00'."""
        parts = [p.strip() for p in raw.split(',')]
        name = parts[0]
        if name not in PAN_VALUES:
            self._warn(t, f"unknown pan value {name!r}")
            return
        ams_fms = 0
        if len(parts) > 1 and parts[1].startswith('$'):
            try:
                ams_fms = int(parts[1][1:], 16)
            except ValueError:
                ams_fms = 0
        t.ams_fms_pan = (PAN_VALUES[name] | (ams_fms & 0x3F)) & 0xFF
        if t.is_fm:
            fm_set_pan(self.opn2, t)

    def _psg_voice_index(self, t: SfxTrack, raw: str) -> int:
        """smpsPSGvoice takes either a hex literal or a symbolic fTone_NN label."""
        raw = raw.strip().rstrip(',')
        if raw.startswith('$'):
            try:
                return int(raw[1:], 16)
            except ValueError:
                pass
        elif raw.lower().startswith('ftone_'):
            try:
                return int(raw.split('_')[1])
            except (IndexError, ValueError):
                pass
        self._warn(t, f"unrecognised smpsPSGvoice operand {raw!r}")
        return 0

    def _voice(self, t: SfxTrack, index: int):
        if not t.is_fm:
            return None
        if index < 0 or index >= len(self.song.voices):
            self._warn(t, f"smpsSetvoice ${index:02X} but the file defines "
                          f"{len(self.song.voices)} voice(s)")
            return None
        return self.song.voices[index]

    def _stop_track(self, t: SfxTrack) -> None:
        """cfStopTrack :2528 — a plain key-off; no release rate is written."""
        t.playing = False
        t.no_attack = False
        if t.is_fm:
            fm_key_off(self.opn2, t)
        else:
            psg_note_off(self.sn, t)

    def _warn(self, t: SfxTrack, message: str) -> None:
        text = f"{t.name}: {message}"
        if text not in self.warnings:
            self.warnings.append(text)
