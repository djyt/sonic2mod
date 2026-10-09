"""A driver's register writes to a playing voice, as voices: Streets of Rage's `$FA r v`.

A track writes one of its voice's operator registers (a decay or release rate); the chip plays the
voice so changed until the next voice set rewrites every register.  The walk (driver_track.py)
sets a patched copy of the voice where each write was: one copy per voice and set of patches,
shared by every track of the song that plays it, so everything after the walk sees voices only.

    SetVoice(3) ... VoiceRegister($6D, $10) ... VoiceRegister($7D, $12)
    SetVoice(3) ... SetVoice(10)               ... SetVoice(11)          (10, 11: voice 3 patched)

A write to a carrier's TL is refused (SmpsVoice.patched): the driver adds the header volume to it
and writes it again after each volume change, which no voice says (no song in the ROM does).
"""

from __future__ import annotations

from .song import SmpsVoice


class VoicePatcher:
    """A song's voices and the patched copies its tracks' register writes make."""

    def __init__(self, voices: list[SmpsVoice]):
        self._voices = {v.index: v for v in voices}
        self._copies: dict[tuple[int, tuple[tuple[int, int], ...]], int] = {}
        self.added: list[SmpsVoice] = []            # the copies, in the order made

    def copy(self, base: int, patches: dict[int, int]) -> int:
        """The index of voice `base` with `patches` (channel 0 register: byte) written over it."""
        key = (base, tuple(sorted(patches.items())))
        if key not in self._copies:
            voice = self._voices[base]
            for register, value in patches.items():
                voice = voice.patched(register, value)
            voice.index = max(self._voices) + 1
            self._voices[voice.index] = voice
            self._copies[key] = voice.index
            self.added.append(voice)
        return self._copies[key]
