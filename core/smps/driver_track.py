"""One track's driver state: what the walk (code.py) asks a track's driver, by its TrackRules.

The walk follows the code (labels, loops, jumps, calls) and builds notes from note and duration
bytes; whatever a driver does to them beyond SMPS 68k Type 1's reading is answered here:

    effect(e)            the effect as the track plays it: a volume step a SetVol, a detune add a
                         Detune, a register write the voice it leaves; None for one the notes take
                         (a gate)
    note(value)          the note byte as it sounds: a drum track's selected sample, a noise note
                         on the tone it is clocked by
    cut(note, tied_next) the note as the driver keys it off: itself, or the part it holds and a rest
    jump_clears_tie      a jump drops a pending tie

A track whose driver states no TrackRules (Sonic 1's) passes everything through.
"""

from __future__ import annotations

import dataclasses

from .driver_tables import signed_byte, signed_word
from .effects import (
    AlterVol,
    AlterVolumeStep,
    Detune,
    DetuneAdd,
    DriverEffect,
    Gate,
    PlayedEffect,
    PsgForm,
    SelectSample,
    SetVoice,
    SetVol,
    SmpsEffect,
    VoiceRegister,
    VolumeStep,
)
from .names import FM_CHANNEL_NAMES
from .rules import PlaybackRules
from .song import MAX_PSG, REST, SELECTED_SAMPLE, ChannelType, SmpsChannelHeader, SmpsNote
from .voice_patch import VoicePatcher

_BYTE = 0x100
_FM_PART = 3                    # channels per YM2612 part: a register's low bits name one of them


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
        self._detune_word = 0
        self._gate = 0                             # frames before a note's end the driver keys it off
        self._noise = False                        # a PSG_FORM ran: notes are noise
        self._tone_note: int | None = None         # the last tone note: what tone 3 still holds
        self._dac_sample: int | None = None        # DAC_SAMPLE's: what a drum track's SELECTED_SAMPLE plays
        self._voice: int | None = None             # the voice set last, and the registers written over it
        self._patches: dict[int, int] = {}

    @property
    def jump_clears_tie(self) -> bool:
        return self._rules.jump_clears_tie

    # --- effects ------------------------------------------------------------------------------

    def effect(self, effect: SmpsEffect) -> PlayedEffect | None:
        """`effect` as the track plays it: a volume step a level (an AlterVol then moves that level
        as the driver keeps it, unclamped), a detune add the detune, a register write the patched
        voice; None: the notes take it (a gate)."""
        match effect:
            case Gate(frames=frames):
                self._gate = frames
                return None
            case VolumeStep(step=step):
                return self._volume(signed_byte(step % _BYTE))
            case AlterVolumeStep(delta=delta):
                return self._volume(signed_byte((self._volume_step + delta) % _BYTE))
            case Detune(offset=offset):
                return self._detune(offset)
            case DetuneAdd(offset=offset):
                return self._detune(self._detune_word + offset)
            case AlterVol(delta=delta) if self._level is not None:
                self._level = signed_byte((self._level + delta) % _BYTE)
                return SetVol(self._level)
            case PsgForm():
                self._noise = True
            case SelectSample(sound=sound):
                self._dac_sample = sound
            case SetVoice(index=index):
                self._voice, self._patches = index, {}
            case VoiceRegister(register=register, value=value):
                return self._patched(register, value)
        if isinstance(effect, DriverEffect):
            raise ValueError(f"{self._header.label}: {effect} is no effect this track's driver plays")
        assert isinstance(effect, PlayedEffect)
        return effect

    def _patched(self, register: int, value: int) -> SetVoice:
        """The voice set with `register` (as the track writes it: its channel in the low bits)
        written over it, and every write since the voice was set."""
        if self._voice is None or self._header.channel_type != ChannelType.FM:
            raise ValueError(f"{self._header.label}: a register write with no FM voice set")
        self._patches[register - self._channel_number()] = value
        return SetVoice(self._voices.copy(self._voice, self._patches))

    def _channel_number(self) -> int:
        """The track's channel within its YM2612 part: what its register writes carry in their low bits."""
        return FM_CHANNEL_NAMES.index(self._name) % _FM_PART

    def _volume(self, step: int) -> SetVol:
        """Volume step `step`: its level in the driver's table, the header volume added (add.b)."""
        level = self._rules.volume_steps.get(step)
        if level is None:
            raise ValueError(f"{self._header.channel_type} track '{self._header.label}': volume step "
                             f"{step}, which its driver has no level for")
        self._volume_step = step
        self._level = signed_byte((level + self._header.volume) % _BYTE)
        return SetVol(self._level)

    def _detune(self, word: int) -> Detune:
        """The detune word `word` (add.w) as the track adds it (the PSG's shifted to a divider)."""
        self._detune_word = signed_word(word)
        return Detune(self._detune_word >> self._rules.detune_shift)

    # --- notes --------------------------------------------------------------------------------

    def note(self, value: int) -> int:
        """A note byte as it sounds: SELECTED_SAMPLE the sample DAC_SAMPLE chose (none yet: the
        driver plays nothing); a noise note whose driver leaves tone 3 alone, the last tone note
        (none: nMaxPSG)."""
        if value == SELECTED_SAMPLE:
            return REST if self._dac_sample is None else self._dac_sample
        if value == REST or not self._is_psg:
            return value
        if not self._noise:
            self._tone_note = value
            return value
        if self._rules.noise_writes_tone3:
            return value
        return MAX_PSG if self._tone_note is None else self._tone_note

    def cut(self, note: SmpsNote, tied_next: bool) -> list[SmpsNote]:
        """`note` (its duration read; `tied_next`: the next byte is a tie) as the driver keys it
        off: itself, or the part it holds and a rest after (both cut; no held part: the rest
        alone).  A gated note at its gate; a rest after a tie where TrackRules.tied_rest_holds."""
        if not note.is_rest:
            if self._gated(note, tied_next):
                return _split(note, note.duration - self._gate)
            return [note]
        holds = self._rules.tied_rest_holds
        if note.is_no_attack and holds is not None and holds < note.duration:
            return _split(note, holds)
        return [note]

    def _gated(self, note: SmpsNote, tied_next: bool) -> bool:
        """The gate keys `note` off: it outlasts the gate, and is not one the driver spares - a
        tied note where the key-off waits on the tie, one the next byte ties where the driver
        looks (it checks each frame)."""
        if not self._gate or note.duration <= self._gate:
            return False
        if note.is_no_attack and self._rules.gate_spares_tied:
            return False
        return not (self._rules.gate_sees_tie and tied_next)


def _split(note: SmpsNote, held: int) -> list[SmpsNote]:
    """`note` keyed off after `held` ticks (0: at once): what is left of it a rest; both cut."""
    rest = SmpsNote(note_value=REST, duration=note.duration - held, is_rest=True, cut=True)
    if not held:
        return [rest]
    return [dataclasses.replace(note, duration=held, cut=True), rest]
