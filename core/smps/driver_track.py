"""One track's driver state: what the walk (code.py) asks a track's driver, by its TrackRules.

The walk follows the code (labels, loops, jumps, calls) and builds notes from note and duration
bytes; whatever a driver does to them beyond SMPS 68k Type 1's reading is answered here:

    effect(e)            the effects it plays as: a volume step a SetVol, a detune add a Detune, a
                         register write or channel 3's special mode the voice it leaves; none for one
                         the notes take (a gate, a soft release); a volume change after a held
                         carrier write the voice back, then the level
    note(value)          the note byte as it sounds: a drum track's selected sample, a noise note
                         on the tone it is clocked by
    before_note()        as a note is read: whether it slides on (a note a soft release spared still
                         keys), and what the driver writes (the level back after a soft release)
    cut(note, tied_next) the note as the driver keys it off, in time order: itself, or the part it
                         holds and a rest (a soft release: the part it holds, its softer level, the
                         rest held); an effect plays where the note before it ends, or the rest's start;
                         under a volume envelope, the level it reads at and the TLs after (LevelPath)
    opening()            what the track plays from its start: the level its driver starts it at
    jump_clears_tie      a jump drops a pending tie; exit_clears_tie: a loop's exit
    ties(direct)         a tie byte (`direct`: straight after its note) ties the next read
    legato               every read ties, as if smpsNoAttack came before it (Legato on)
    duration(ticks)      a duration (byte x divider) as the track counts it

A track whose driver states no TrackRules (Sonic 1's) passes everything through.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from ..chips import CH3_CHANNEL, NOISE_RATE_MASK, TL_MASK, OperatorReg
from .adsr import Adsr
from .driver_tables import SMPS_OP_TO_REG_OFFSET, signed_byte, signed_word
from .effects import (
    AdsrEnvelope,
    AlterVol,
    AlterVolumeStep,
    Detune,
    DetuneAdd,
    DriverEffect,
    EnvelopePreset,
    Fm3Special,
    Gate,
    Glide,
    KeyOffAfter,
    Legato,
    LevelPath,
    NoiseRate,
    PlayedEffect,
    Portamento,
    PsgForm,
    PsgVoice,
    ScaledVolume,
    SelectSample,
    SetVoice,
    SetVol,
    SmpsEffect,
    SoftRelease,
    VoiceRegister,
    VolumeEnvelope,
    VolumeMask,
    VolumeStep,
)
from .names import FM_CHANNEL_NAMES
from .rules import PlaybackRules
from .song import FIRST_NOTE, MAX_PSG, REST, SELECTED_SAMPLE, ChannelType, SmpsChannelHeader, SmpsNote
from .voice_patch import NO_CHANGES, VoicePatcher
from .volume_envelope import EnvelopeTrack

_BYTE_VALUES = 0x100

# What a key-off leaves, in time order: the notes it splits a note into, and the effects between them
KeyedOff = SmpsNote | PlayedEffect
_FM_PART = 3                    # channels per YM2612 part: a register's low bits name one of them
_SPECIAL_MODE_CHANNEL = FM_CHANNEL_NAMES[CH3_CHANNEL]
_TL_REGISTERS = frozenset(OperatorReg.TL + offset for offset in SMPS_OP_TO_REG_OFFSET)   # channel 0's four


class DriverTrack:
    """A track's state as its driver keeps it, read by its kind's TrackRules."""

    def __init__(self, header: SmpsChannelHeader, name: str, rules: PlaybackRules, voices: VoicePatcher):
        self._header = header
        self._name = name                          # the chip channel it plays on: "FM4" ...
        self._voices = voices                      # the song's: a register write's patched copy
        self._rules = rules.track(header.channel_type)
        self._is_psg = header.channel_type == ChannelType.PSG
        self._volume_step = 0                      # the RAM starts cleared
        self._level: int | None = None             # the level a volume step set, as the driver keeps it
        self._soft: int | None = None              # a soft release's n while it is on
        self._soft_n = 0                           # the n set last
        self._spared = False                       # a soft release spared the last note's key-off: it still keys
        self._softened = False                     # ... at the soft release's level
        self._voice_loads = False                  # a voice set this read loads after it, keying a spared note off
        self._adsr: Adsr | None = None             # the ADSR envelope set (EnvelopePreset, AdsrEnvelope)
        self._presets = rules.adsr_presets
        self._detune_word = 0
        self._gate = 0                             # frames before a note's end the driver keys it off
        self._after = 0                            # ... after a read starts (KeyOffAfter), a gate first
        self._sounding = False                     # a note sounds (TrackRules.key_off.rest_holds)
        self._scaled = 0                           # ScaledVolume's level: the track RAM starts cleared
        self._written: int | None = None           # ... as the levels last played write it
        self._mask: int | None = None              # VolumeMask's operators; None: the carriers
        envelopes = self._rules.volume.envelopes
        self._level_envelope = None if envelopes is None else EnvelopeTrack(envelopes)
        self._legato = False                       # every read ties (Legato)
        self._noise_form: int | None = None        # the PSG_FORM that holds: notes are noise
        self._tone_note: int | None = None         # the last tone note: what tone 3 still holds
        self._shift = self._rules.pitch.word_shift  # the detune word's shift, as the last note set it
        self._next_shift = self._shift             # ... as the note being read sets it
        self._dac_sample: int | None = None        # DAC_SAMPLE's: what a drum track's SELECTED_SAMPLE plays
        self._voice: int | None = None             # the voice set last, and what the track plays it with
        self._changes = NO_CHANGES
        self._read_levels: dict[int, int] = {}     # the TLs written in this read (by register): queued, after its volume

    @property
    def jump_clears_tie(self) -> bool:
        return self._rules.ties.jump_clears

    def opening(self) -> tuple[PlayedEffect, ...]:
        """What the track plays from its start before any flag: the level its driver starts it at."""
        initial = self._rules.volume.initial_step
        return () if initial is None else (self._volume(initial),)

    def ties(self, direct: bool) -> bool:
        """A tie byte ties the next read: straight after its note, or where the driver looks for it
        at the next read.  One after a flag comes too late where the note's end keyed it off first
        (TrackRules.ties.late_keys_off), unless a soft release spared the key-off: the next note
        slides on (Streets of Rage 2's, $4156C: no key-on after a tie)."""
        return direct or not self._rules.ties.late_keys_off or self._soft is not None

    @property
    def exit_clears_tie(self) -> bool:
        return self._rules.ties.exit_clears

    @property
    def legato(self) -> bool:
        return self._legato

    # --- effects ------------------------------------------------------------------------------

    def effect(self, effect: SmpsEffect) -> tuple[PlayedEffect, ...]:
        """`effect` as the track plays it: a volume step a level (an AlterVol then moves that level
        as the driver keeps it, unclamped), a detune add the detune, a register write or special
        mode the patched voice; none: the notes take it (a gate)."""
        match effect:
            case Gate(frames=frames):
                self._gate = frames
                return ()
            case KeyOffAfter(frames=frames):
                self._after = frames
                return ()
            case ScaledVolume(level=level):
                self._scaled = level
                if self._level_envelope is not None:
                    self._level_envelope.stop()
                return self._scaled_levels()
            case VolumeEnvelope() if self._level_envelope is not None:
                self._level_envelope.set(effect)
                return ()
            case VolumeMask(operators=operators):
                self._mask = operators
                return self._scaled_levels()
            case Legato():
                self._legato = effect.on
                return ()
            case VolumeStep(step=step):
                byte = signed_byte(step % _BYTE_VALUES)
                return self._volume_change(self._volume(self._rules.volume.kept.get(byte, byte)))
            case AlterVolumeStep(delta=delta):
                return self._volume_change(self._volume(self._altered(delta)))
            case SoftRelease(level=level, on=on):
                return self._soft_release(level, on)
            case EnvelopePreset(preset=preset):
                return self._envelope(self._preset(preset))
            case AdsrEnvelope():
                return self._envelope(Adsr(*effect.values))
            case Detune(offset=offset):
                return (self._detune(offset),)
            case DetuneAdd(offset=offset):
                return (self._detune(self._detune_word + offset),)
            case AlterVol(delta=delta) if self._level is not None:
                self._level = signed_byte((self._level + delta) % _BYTE_VALUES)
                return self._volume_change(SetVol(self._level))
            case PsgForm(noise=noise):
                self._noise_form = noise
            case NoiseRate(rate=rate):
                # A rate outside noise mode writes nothing the noise plays (Streets of Rage 2's $F8)
                if self._noise_form is None:
                    return ()
                self._noise_form = self._noise_form & ~NOISE_RATE_MASK | rate
                return (PsgForm(self._noise_form),)
            case SelectSample(sound=sound):
                self._dac_sample = sound
            case SetVoice(index=index):
                # A voice set rewrites every register; the special mode stays.  Under a soft release
                # it may key the note it spared off
                law = self._rules.key_off.soft_release
                self._voice_loads = law is not None and law.voice_keys_off
                self._voice = index
                self._changes = dataclasses.replace(self._changes, registers=(), levels=())
                if self._rules.volume.law is not None:
                    self._mask = None                  # the voice set masks its carriers (Time Trax's $0F91)
                    return self._scaled_levels()
                return (SetVoice(self._voices.copy(index, self._changes)),)
            case VoiceRegister(register=register, value=value):
                return (self._patched(register, value),)
            case Fm3Special(offsets=offsets):
                voice = self._special_mode(offsets)
                return () if voice is None else (voice,)
            case Glide() | Portamento() if self._rules.pitch.slide is None:
                raise ValueError(f"{self._header.label}: {effect}: this track's driver slides no pitch")
        if isinstance(effect, DriverEffect):
            raise ValueError(f"{self._header.label}: {effect} is no effect this track's driver plays")
        assert isinstance(effect, PlayedEffect)
        return (effect,)

    def _patched(self, register: int, value: int) -> SetVoice:
        """The voice set with `register` (as the track writes it: its channel in the low bits)
        written over it, and every write since the voice was set.  A TL the volume sets, where the
        driver holds it, until the next volume change."""
        if self._voice is None or self._header.channel_type != ChannelType.FM:
            raise ValueError(f"{self._header.label}: a register write with no FM voice set")
        register -= self._channel_number()
        if self._rules.volume.tl_writes_hold and register in _TL_REGISTERS:
            self._changes = self._changes.level_written(register, value)
            self._read_levels[register] = value
        else:
            self._changes = self._changes.written(register, value)
        return SetVoice(self._voices.copy(self._voice, self._changes))

    def _preset(self, preset: int) -> Adsr:
        if preset >= len(self._presets):
            raise ValueError(f"{self._header.label}: ADSR preset {preset}, which its driver has none of")
        return self._presets[preset]

    def _envelope(self, adsr: Adsr) -> tuple[PlayedEffect, ...]:
        """An ADSR envelope from the next note on: as the envelope at the track's volume."""
        self._adsr = adsr
        return (PsgVoice(adsr.name(self._volume_step)),)

    def _altered(self, delta: int) -> int:
        """The step an AlterVolumeStep leaves: the sum clamped where the driver clamps it, else a signed byte."""
        total = self._volume_step + delta
        clamp = self._rules.volume.step_range
        if clamp is None:
            return signed_byte(total % _BYTE_VALUES)
        return min(max(total, clamp.start), clamp.stop - 1)

    def _soft_release(self, level: int | None, on: bool) -> tuple[PlayedEffect, ...]:
        """A soft release on (its n, or the last one) or off: off, a note ringing softened is back
        at the track's level (the driver rewrites the volume, $41BCC)."""
        if self._rules.key_off.soft_release is None:
            raise ValueError(f"{self._header.label}: a soft release, which this track's driver has none of")
        if level is not None:
            self._soft_n = level
        self._soft = self._soft_n if on else None
        if on or not self._softened:
            return ()
        return self._restored()

    def _restored(self) -> tuple[PlayedEffect, ...]:
        """The track's level written again, over a soft release's."""
        level = None if self._level is None else SetVol(self._level)
        return self._volume_written(level if self._softened else None)

    def _volume_change(self, level: SetVol) -> tuple[PlayedEffect, ...]:
        """A volume change, written at once; under an ADSR envelope, the envelope at the new volume."""
        written = self._volume_written(level)
        if self._adsr is None:
            return written
        return (*written, PsgVoice(self._adsr.name(self._volume_step)))

    def _volume_written(self, level: SetVol | None) -> tuple[PlayedEffect, ...]:
        """The driver writes the volume (`level`; None: the level it has): the voice back from
        the TL writes it held, where its volume write rewrites every TL ($41A9A), then the level.
        Those of the same read stay: the driver writes its queued registers after the volume ($41112)."""
        self._softened = False
        played = () if level is None else (level,)
        held = tuple(sorted(self._read_levels.items()))
        if self._changes.levels == held:
            return played
        assert self._voice is not None
        self._changes = dataclasses.replace(self._changes, levels=held)
        return (SetVoice(self._voices.copy(self._voice, self._changes)), *played)

    def _scaled_levels(self) -> tuple[PlayedEffect, ...]:
        """The voice and level a law that scales operators writes (TrackRules.volume.law): a SetVol of
        the loudest carrier's change, the voice copied with each TL that change misses written exact.

            TLs (carriers C) 10C 40C 20, volume 200:  scaled 36 59 20  ->  SetVol 26, TL $44 59 fixed
        """
        if self._voice is None:
            return ()
        self._written = self._scaled
        voice = self._voices.voice(self._voice)
        tls = {r: v for r, v in voice.chip_registers().items() if r in _TL_REGISTERS}
        carriers = voice.carrier_registers
        scaled = self._law_levels(self._scaled)
        loudest = min(carriers, key=lambda r: scaled[r])
        offset = scaled[loudest] - tls[loudest]
        added = {r: (v + offset) & TL_MASK if r in carriers else v for r, v in tls.items()}
        missed = tuple((r, scaled[r]) for r in sorted(tls) if scaled[r] != added[r])
        self._changes = dataclasses.replace(self._changes, levels=missed)
        return SetVoice(self._voices.copy(self._voice, self._changes)), SetVol(offset)

    def _law_levels(self, volume: int) -> dict[int, int]:
        """Each TL the law writes for the voice set at `volume` (TrackRules.volume.law)."""
        law = self._rules.volume.law
        if law is None:
            raise ValueError(f"{self._header.label}: a scaled volume, which its driver has no law for")
        assert self._voice is not None
        voice = self._voices.voice(self._voice)
        tls = {r: v for r, v in voice.chip_registers().items() if r in _TL_REGISTERS}
        return law.levels(tls, voice.carrier_registers, volume, self._mask)

    def _special_mode(self, offsets: tuple[int, ...] | None) -> SetVoice | None:
        """The voice set, its operators at `offsets` from the next note on (None: before any
        voice).  Only channel 3 has the mode: the driver keys no other track's frequency in it."""
        if self._name != _SPECIAL_MODE_CHANNEL:
            raise ValueError(f"{self._header.label}: channel 3's special mode on {self._name}")
        self._changes = dataclasses.replace(self._changes, fnum_offsets=offsets)
        if self._voice is None:
            return None
        return SetVoice(self._voices.copy(self._voice, self._changes))

    def _channel_number(self) -> int:
        """The track's channel within its YM2612 part: what its register writes carry in their low bits."""
        return FM_CHANNEL_NAMES.index(self._name) % _FM_PART

    def _volume(self, step: int) -> SetVol:
        """Volume step `step` kept: its level."""
        self._volume_step = step
        self._level = self._step_level(step)
        return SetVol(self._level)

    def _step_level(self, step: int) -> int:
        """Step `step`'s level in the driver's table, the header volume added (add.b)."""
        level = self._rules.volume.steps.get(step)
        if level is None:
            raise ValueError(f"{self._header.channel_type} track '{self._header.label}': volume step "
                             f"{step}, which its driver has no level for")
        return signed_byte((level + self._header.volume) % _BYTE_VALUES)

    def _detune(self, word: int) -> Detune:
        """The detune word `word` (add.w) as the track adds it (the PSG's shifted to a divider)."""
        self._detune_word = signed_word(word)
        return Detune(self._detune_word >> self._shift)

    def duration(self, ticks: int) -> int:
        """A duration (byte x divider; 0: none read yet) as the track counts it: its low byte,
        0 lasting 256 ticks, where TrackRules.byte_durations."""
        if not self._rules.byte_durations:
            return ticks
        return (ticks - 1) % _BYTE_VALUES + 1

    # --- notes --------------------------------------------------------------------------------

    def note(self, value: int) -> int:
        """A note byte as it sounds: SELECTED_SAMPLE the sample DAC_SAMPLE chose (none yet: the
        driver plays nothing); a noise note whose driver leaves tone 3 alone, the last tone note
        (none: nMaxPSG)."""
        if value == SELECTED_SAMPLE:
            return REST if self._dac_sample is None else self._dac_sample
        if value == REST or not self._is_psg:
            return value
        self._next_shift = self._rules.pitch.shift(value - FIRST_NOTE)
        if self._noise_form is None:
            self._tone_note = value
            return value
        if self._rules.pitch.noise_writes_tone3:
            return value
        return MAX_PSG if self._tone_note is None else self._tone_note

    def before_note(self) -> tuple[bool, tuple[PlayedEffect, ...]]:
        """As a note (not a rest) is read: whether it slides on, and what the driver writes first.
        A soft release keys a note it spared off before the next ($4156C); with it off since, the
        key-on comes alone ($415E0) and the note still keying slides on, no attack.  A softened
        note's level is back for the new one ($40F94: the volume rewritten)."""
        self._load_voice()
        law = self._rules.key_off.soft_release
        if self._adsr is not None and law is not None and law.envelope_clears:
            self._soft = None
        slides = self._spared and self._soft is None
        written = self._restored() if self._soft is not None or self._softened else ()
        return slides, (*self._shifted(), *written)

    def _shifted(self) -> tuple[PlayedEffect, ...]:
        """The detune again where the note read shifts it otherwise (by its octave)."""
        if self._next_shift == self._shift:
            return ()
        self._shift = self._next_shift
        return (Detune(self._detune_word >> self._shift),) if self._detune_word else ()

    def _load_voice(self) -> None:
        """The read ends: a voice set in it loads, keying the note a soft release spared off if
        the soft release is still on."""
        if self._voice_loads and self._soft is not None:
            self._spared = False
        self._voice_loads = False

    def cut(self, note: SmpsNote, tied_next: bool) -> Sequence[KeyedOff]:
        """`note` (its duration read; `tied_next`: the next byte is a tie) as the driver keys it
        off, in time order: itself, or the part it holds and a rest after (both cut; no held part:
        the rest alone).  A gated note at its gate; a rest after a tie where
        TrackRules.key_off.tied_rest_holds.  Under a soft release nothing keys off: at the gate,
        or the rest after a note, the note drops to its softer level (a SetVol) and rings on, a
        rest held.  An effect plays where the note before it ends, or a rest's first where it starts."""
        self._read_levels = {}                     # the read is over: its writes are the chip's
        played = self._keyed_off(note, tied_next)
        return played if self._level_envelope is None else self._enveloped(note, played)

    def _keyed_off(self, note: SmpsNote, tied_next: bool) -> Sequence[KeyedOff]:
        if self._rules.key_off.rest_holds:
            return self._holding(note, tied_next)
        if not note.is_rest:
            self._spared = self._soft is not None
            held = self._held(note, tied_next)
            if held is None:
                return [note]
            if not self._spared:
                return _split(note, held)
            rest = _split(note, held)[-1]
            return [dataclasses.replace(note, duration=held, cut=True), *self._softened_level(),
                    dataclasses.replace(rest, is_no_attack=True)]
        self._load_voice()
        if self._soft is not None:
            # The note before rings on through the rest (unless a voice set keyed it off); at its
            # end (no gate) it softened
            if not self._spared:
                return [note]
            held_rest = dataclasses.replace(note, is_no_attack=True)
            return [held_rest] if self._softened else [*self._softened_level(), held_rest]
        self._spared = False
        holds = self._rules.key_off.tied_rest_holds
        if note.is_no_attack and holds is not None and holds < note.duration:
            return _split(note, holds)
        return [note]

    def _enveloped(self, read: SmpsNote, played: Sequence[KeyedOff]) -> list[KeyedOff]:
        """`played` under a volume envelope: the level its first note reads at (the voice and SetVol
        where it changed), and the TLs on its frames after (LevelPath).  A read's duration counts
        frames (a driver with no tempo: Time Trax's)."""
        assert self._level_envelope is not None
        volumes = self._level_envelope.volumes(self._scaled, read.duration, note=not read.is_rest)
        first = next(p for p in played if isinstance(p, SmpsNote))
        lead: tuple[PlayedEffect, ...] = ()
        if self._voice is not None and (not first.is_rest or first.is_no_attack):
            self._scaled = volumes[0]
            path = self._level_path(volumes[:first.duration])
            lead = (*(self._scaled_levels() if self._scaled != self._written else ()),
                    *((LevelPath(path),) if path else ()))
        self._scaled = volumes[-1]
        return [*lead, *played]

    def _level_path(self, volumes: tuple[int, ...]) -> tuple[tuple[int, tuple[tuple[int, int], ...]], ...]:
        """The TLs at each change after the first frame, by the law."""
        at: dict[int, tuple[tuple[int, int], ...]] = {}
        path, last = [], None
        for frame, volume in enumerate(volumes):
            if volume not in at:
                at[volume] = tuple(sorted(self._law_levels(volume).items()))
            if frame and at[volume] != last:
                path.append((frame, at[volume]))
            last = at[volume]
        return tuple(path)

    def _holding(self, note: SmpsNote, tied_next: bool) -> list[SmpsNote]:
        """`note` where a rest writes nothing (TrackRules.key_off.rest_holds): a rest, a tie or a
        legato read keys nothing on, so it ties the note still sounding, or rests after a key-off;
        every read is keyed off where its gate or KeyOffAfter falls (Time Trax's, $1293).

            KeyOffAfter 2:  C 6                ->  C 2 | rest 4
            Gate 3:         C 2, rest 6        ->  C 2 | C tied 3 | rest 3
        """
        if note.is_rest or note.is_no_attack:
            if not self._sounding:
                return [SmpsNote(note_value=REST, duration=note.duration, is_rest=True)]
            note = dataclasses.replace(note, is_no_attack=True)
        held = self._held(note, tied_next)
        played = [note] if held is None else _split(note, held)
        self._sounding = not played[-1].is_rest
        return played

    def _softened_level(self) -> tuple[PlayedEffect, ...]:
        """The level a soft release drops a note to, written as a volume: that of the step it
        makes of the track's."""
        law = self._rules.key_off.soft_release
        assert law is not None and self._soft is not None
        written = self._volume_written(SetVol(self._step_level(law.step(self._volume_step, self._soft))))
        self._softened = True
        return written

    def _held(self, note: SmpsNote, tied_next: bool) -> int | None:
        """Ticks the gate leaves `note` keyed (None: it plays whole): it outlasts the gate, or
        is no longer than it where the driver keys such a note a floor of frames; not one the
        driver spares - a tied note where the key-off waits on the tie, one the next byte ties
        where the driver looks (it checks each frame)."""
        if not self._gate:
            return self._after if 0 < self._after < note.duration else None
        if note.is_no_attack and self._rules.key_off.gate_spares_tied:
            return None
        if self._rules.key_off.gate_sees_tie and tied_next:
            return None
        if note.duration > self._gate:
            return note.duration - self._gate
        floor = self._rules.key_off.gate_floor
        return floor if floor is not None and floor < note.duration else None


def _split(note: SmpsNote, held: int) -> list[SmpsNote]:
    """`note` keyed off after `held` ticks (0: at once): what is left of it a rest; both cut."""
    rest = SmpsNote(note_value=REST, duration=note.duration - held, is_rest=True, cut=True)
    if not held:
        return [rest]
    return [dataclasses.replace(note, duration=held, cut=True), rest]
