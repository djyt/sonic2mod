"""What a drum track's bytes play when they are not DAC samples: FM drum programs.

A Type 0 FM drum track names a drum per note (low nibble); the driver plays that drum's program -
a short track of its own - on FM3 with its own voice.  A hit is 1-6 frames of tied pitch steps and
slides, below a MOD row, so it is rendered whole as a one-shot sample (docs/todo/binary_import.md
3.5).  The reader runs the program frame by frame as the driver does; the frames are the chip's.

    FmDrum(voice, tl_offset, frames)      frames[n]: what FM3 holds on V-int frame n of the hit
        FmFrame(word, keyed, attack)      word: block << 11 | fnum; attack: keyed on this frame
"""

from __future__ import annotations

from dataclasses import dataclass

from .song import SmpsVoice


@dataclass(frozen=True)
class FmFrame:
    word: int            # the frequency word written this frame (block << 11 | fnum)
    keyed: bool          # the channel is keyed on after this frame's writes
    attack: bool = False  # a key-on this frame (after a key-off when it was keyed)


@dataclass(frozen=True)
class FmDrum:
    voice: SmpsVoice
    tl_offset: int                 # the carrier TL the drum adds to its voice's (the drum's volume)
    frames: tuple[FmFrame, ...]    # from the hit to the program's stop (keyed off), or the read's cap
    cut: str = ""                  # why the frames end before a stop ("" when the program stopped)

    @property
    def silent(self) -> bool:
        """The program never keys on (Golden Axe's drum89: a rest): a hit only stops the drum before it."""
        return not any(f.attack for f in self.frames)
