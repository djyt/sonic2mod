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

from .song import SmpsVoice

_Key = tuple[int, tuple[tuple[int, int], ...], tuple[int, ...] | None]


class VoicePatcher:
    """A song's voices and the patched copies its tracks' register writes make."""

    def __init__(self, voices: list[SmpsVoice]):
        self._voices = {v.index: v for v in voices}
        self._copies: dict[_Key, int] = {}
        self.added: list[SmpsVoice] = []            # the copies, in the order made

    def copy(self, base: int, patches: dict[int, int], fnum_offsets: tuple[int, ...] | None = None) -> int:
        """The index of voice `base` with `patches` (channel 0 register: byte) written over it,
        playing at `fnum_offsets` (channel 3's special mode; None: normal); `base` itself where
        there is neither."""
        if not patches and fnum_offsets is None:
            return base
        key = (base, tuple(sorted(patches.items())), fnum_offsets)
        if key not in self._copies:
            voice = dataclasses.replace(self._voices[base], fnum_offsets=fnum_offsets)
            for register, value in patches.items():
                voice = voice.patched(register, value)
            voice.index = max(self._voices) + 1
            self._voices[voice.index] = voice
            self._copies[key] = voice.index
            self.added.append(voice)
        return self._copies[key]
