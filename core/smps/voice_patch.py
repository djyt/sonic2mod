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

A write to a carrier's TL is refused (SmpsVoice.patched): the driver adds the header volume to it
and writes it again after each volume change, which no voice says (no song in the ROM does).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .song import SmpsVoice


@dataclass(frozen=True)
class VoiceChanges:
    """What a track plays its voice with beyond the voice's own bytes."""
    registers: tuple[tuple[int, int], ...] = ()     # (channel 0 register, byte) written over it, by register
    fnum_offsets: tuple[int, ...] | None = None     # channel 3's special mode; None: normal

    def written(self, register: int, value: int) -> VoiceChanges:
        """These changes and `register` written `value`."""
        registers = dict(self.registers) | {register: value}
        return dataclasses.replace(self, registers=tuple(sorted(registers.items())))

    def applied(self, voice: SmpsVoice) -> SmpsVoice:
        """A copy of `voice` playing with these changes."""
        voice = dataclasses.replace(voice, fnum_offsets=self.fnum_offsets)
        for register, value in self.registers:
            voice = voice.patched(register, value)
        return voice


NO_CHANGES = VoiceChanges()


class VoicePatcher:
    """A song's voices and the patched copies its tracks' changes make."""

    def __init__(self, voices: list[SmpsVoice]):
        self._voices = {v.index: v for v in voices}
        self._copies: dict[tuple[int, VoiceChanges], int] = {}
        self.added: list[SmpsVoice] = []            # the copies, in the order made

    def copy(self, base: int, changes: VoiceChanges) -> int:
        """The index of voice `base` played with `changes`: `base` itself for none."""
        if changes == NO_CHANGES:
            return base
        key = (base, changes)
        if key not in self._copies:
            voice = changes.applied(self._voices[base])
            voice.index = max(self._voices) + 1
            self._voices[voice.index] = voice
            self._copies[key] = voice.index
            self.added.append(voice)
        return self._copies[key]
