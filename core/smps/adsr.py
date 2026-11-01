"""A software ADSR envelope, as MUCOM88's SSG soft envelope keeps one (Streets of Rage 2's PSG: `$F0 n`
a preset, `$FA` six bytes): a level 0-255 the track's volume is scaled by, stepped once a frame
from the note's read on.

    attack    from AL, + AR a frame up to 255            (the read's frame steps first)
    decay     - DR a frame down to SL
    sustain   - SR a frame down to 0                     (SR 0: held)
    release   at the gate, - RR a frame down to 0
    att       ((level x (v + 1)) >> 8) ^ 15              (v the volume, 0-15)

          255 |  /\\
              | /  \\________
           SL |/            \\___  sustain       (release from the gate on)
              +---------------------------> frames

Each (envelope, volume) is one PSG envelope as the walk and the converter know them: its
attenuations over the volume's own (v ^ 15), named by its six bytes and the volume
(adsr_ffffffc8000a_v12).  One that never drops below the volume plays as none ("$00").
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import astuple, dataclass

from .driver_tables import PsgEnvelope, psg_voice_name

_FULL = 0xFF
_SILENT = 0xF                   # the attenuation of a level of 0, and what v ^ 15 is taken from
_LEVEL_SHIFT = 8
_ADSR_BYTES = 6
_PREFIX = "adsr_"
_VOLUME_MARK = "_v"
NO_ENVELOPE = psg_voice_name(0)     # "$00": a note at its volume alone


@dataclass(frozen=True)
class Adsr:
    """An envelope's six bytes, in the driver's order."""

    attack_level: int
    attack_rate: int
    decay_rate: int
    sustain_level: int
    sustain_rate: int
    release_rate: int

    @classmethod
    def of(cls, data: bytes) -> Adsr:
        if len(data) != _ADSR_BYTES:
            raise ValueError(f"an ADSR envelope is {_ADSR_BYTES} bytes, not {len(data)}")
        return cls(*data)

    def levels(self) -> list[int]:
        """The level each frame from the read plays, until it holds (the last one held)."""
        level, out = self.attack_level, []
        while True:                                         # attack: the read's frame steps first
            level = min(_FULL, level + self.attack_rate)
            out.append(level)
            if level == _FULL:
                break
        while level != self.sustain_level:                  # decay, to SL
            level = max(self.sustain_level, level - self.decay_rate)
            out.append(level)
        while level and self.sustain_rate:                  # sustain, to 0 (SR 0: held)
            level = max(0, level - self.sustain_rate)
            out.append(level)
        return out

    def release_levels(self, level: int) -> list[int]:
        """The levels after the gate, from `level`, down to 0."""
        frames = math.ceil(level / self.release_rate) if self.release_rate else 0
        return [max(0, level - self.release_rate * (i + 1)) for i in range(frames)]

    def name(self, volume: int) -> str:
        """The envelope at `volume`: its bytes and the volume; none where it never drops below it."""
        envelope = self.at(volume)
        if not any(envelope.steps) and not envelope.release:
            return NO_ENVELOPE
        return f"{_PREFIX}{bytes(astuple(self)).hex()}{_VOLUME_MARK}{volume}"

    def at(self, volume: int) -> PsgEnvelope:
        """The attenuations over the volume's own, a frame at a time, and the release's from the
        sustain level (one of a frame: none, a cut)."""
        release = self.release_levels(self.sustain_level)
        return PsgEnvelope(tuple(_step(level, volume) for level in self.levels()),
                           release=tuple(_step(level, volume) for level in release) if len(release) > 1 else ())


def _step(level: int, volume: int) -> int:
    """A level's attenuation at `volume`, over the volume's own."""
    return (((level * (volume + 1)) >> _LEVEL_SHIFT) ^ _SILENT) - (volume ^ _SILENT)


class AdsrEnvelopes(Mapping[str, PsgEnvelope]):
    """Every ADSR envelope at every volume, by name (Adsr.name), made as asked for: a driver's
    PlaybackRules.psg_envelopes where its tracks name them (no listing)."""

    def __getitem__(self, name: str) -> PsgEnvelope:
        if not name.startswith(_PREFIX) or _VOLUME_MARK not in name:
            raise KeyError(name)
        data, _, volume = name[len(_PREFIX):].partition(_VOLUME_MARK)
        try:
            return Adsr.of(bytes.fromhex(data)).at(int(volume))
        except ValueError as e:
            raise KeyError(name) from e

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return "AdsrEnvelopes()"


ADSR_ENVELOPES = AdsrEnvelopes()
