"""A driver's writes to a playing voice, as voices: Streets of Rage's `$FA r v` and `$F7`.

A track writes one of its voice's operator registers (a decay or release rate); the chip plays the
voice so changed until the next voice set rewrites every register.  Channel 3's special mode
(`$F7`) gives each operator its own frequency offset until the next `$F7`, across voice sets.  The
walk (driver_track.py) sets a patched copy of the voice where each write was: one copy per voice,
set of patches and offsets, shared by every track of the song that plays it, so everything after
the walk sees voices only.

    SetVoice(3) ... VoiceRegister($6D, $10) ... VoiceRegister($7D, $12) ... Fm3Special(100, 0, 0, 0)
    SetVoice(3) ... SetVoice(10)               ... SetVoice(11)            ... SetVoice(12)
                                                       (10, 11, 12: voice 3 patched; 12 offset too)

A write to a TL the volume sets (a carrier's) is refused (SmpsVoice.patched), unless the track's
driver holds it until the next volume change (TrackRules.volume.tl_writes_hold: Streets of Rage 2's,
whose volume write rewrites every TL, a modulator's too): then the copy's TL is the written one, a
carrier's out of the volume's reach (SmpsVoice.level_fixed), until the walk takes it back.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from ..chips import FmLfo
from .song import SmpsVoice


@dataclass(frozen=True)
class VoiceChanges:
    """What a track plays its voice with beyond the voice's own bytes."""
    registers: tuple[tuple[int, int], ...] = ()     # (channel 0 register, byte) written over it, by register
    fnum_offsets: tuple[int, ...] | None = None     # channel 3's special mode; None: normal
    lfo: FmLfo | None = None                        # the hardware LFO it plays under; None: none
    levels: tuple[tuple[int, int], ...] = ()        # (TL register, byte) written until the next volume, by register

    def written(self, register: int, value: int) -> VoiceChanges:
        """These changes and `register` written `value`."""
        registers = dict(self.registers) | {register: value}
        return dataclasses.replace(self, registers=tuple(sorted(registers.items())))

    def level_written(self, register: int, value: int) -> VoiceChanges:
        """These changes and TL `register` written `value`, until the next volume."""
        levels = dict(self.levels) | {register: value}
        return dataclasses.replace(self, levels=tuple(sorted(levels.items())))

    def applied(self, voice: SmpsVoice) -> SmpsVoice:
        """A copy of `voice` playing with these changes."""
        voice = dataclasses.replace(voice, fnum_offsets=self.fnum_offsets, lfo=self.lfo)
        for register, value in self.registers:
            voice = voice.patched(register, value)
        for register, value in self.levels:
            fixed = voice.level_fixed if register in voice.volume_registers else voice.patched
            voice = fixed(register, value)
        return voice


NO_CHANGES = VoiceChanges()


class VoicePatcher:
    """A song's voices and the patched copies its tracks' changes make."""

    def __init__(self, voices: list[SmpsVoice]):
        self._voices = {v.index: v for v in voices}
        self._copies: dict[tuple[int, VoiceChanges], int] = {}
        self._made: dict[int, tuple[int, VoiceChanges]] = {}    # {copy: (its voice, its changes)}
        self.added: list[SmpsVoice] = []            # the copies, in the order made

    def voice(self, index: int) -> SmpsVoice:
        """Voice `index`: the song's own or a copy."""
        return self._voices[index]

    def copy(self, base: int, changes: VoiceChanges) -> int:
        """The index of voice `base` played with `changes`: `base` itself for none."""
        if changes == NO_CHANGES:
            return base
        key = (base, changes)
        if key not in self._copies:
            plain = dataclasses.replace(changes, fnum_offsets=None, lfo=None)
            plain_index = self.copy(base, plain) if plain != changes else None
            voice = changes.applied(self._voices[base])
            voice.plain = plain_index
            voice.unpatched = base if plain_index is None else None
            voice.index = max(self._voices) + 1
            self._voices[voice.index] = voice
            self._copies[key] = voice.index
            self._made[voice.index] = key
            self.added.append(voice)
        return self._copies[key]

    def under_lfo(self, index: int, lfo: FmLfo | None) -> int:
        """Voice `index` (a copy or not) played under `lfo` (None: under none)."""
        base, changes = self._made.get(index, (index, NO_CHANGES))
        return self.copy(base, dataclasses.replace(changes, lfo=lfo))
