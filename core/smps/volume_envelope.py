"""EnvelopeTrack: a track's volume envelope through its reads (TrackRules.volume.envelopes): which
one a note starts, and the track's volume on each frame of a read.

    VolumeEnvelope(a)          each note read from here starts envelope a
    VolumeEnvelope(None, ...)  each note starts the one set last, or (each_note False) none: a
                               running one plays out
    ScaledVolume               stops it (Time Trax's $8A)

    a note's read    starts it again (each_note), then one frame of it
    a rest's read    nothing: the driver writes nothing (Time Trax's $0D18)
    each frame after one frame of it
"""

from __future__ import annotations

from .effects import VolumeEnvelope
from .rules import EnvelopeRun, VolumeEnvelopes


class EnvelopeTrack:
    def __init__(self, envelopes: VolumeEnvelopes) -> None:
        self._envelopes = envelopes
        self._attack: int | None = None             # the envelope a note starts; None: off
        self._each_note = False
        self._run: EnvelopeRun | None = None

    def set(self, effect: VolumeEnvelope) -> None:
        if effect.attack is not None:
            self._attack = effect.attack
        self._each_note = effect.each_note

    def stop(self) -> None:
        self._attack, self._run = None, None

    def volumes(self, volume: int, frames: int, note: bool) -> tuple[int, ...]:
        """The volume on each of a read's `frames` frames, from `volume` (`note`: a note's read)."""
        if note and self._each_note and self._attack is not None:
            self._run = self._envelopes.start(self._attack)
        out = [self._frame(volume) if note else volume]
        for _ in range(frames - 1):
            out.append(self._frame(out[-1]))
        return tuple(out)

    def _frame(self, volume: int) -> int:
        if self._run is None or self._attack is None:
            return volume
        volume = self._run.frame(volume)
        if self._run.done:
            self._run = None
        return volume
