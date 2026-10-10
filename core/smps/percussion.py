"""What a drum track's bytes play when they are not DAC samples: FM drums, with the PSG part a
driver plays beside them.

A Type 0 FM drum track names a drum per note (low nibble); the driver plays that drum's program -
a short track of its own - on FM3 with its own voice.  Space Harrier II's drum byte is a bitmask:
two operator pairs of FM3 in special mode, each its own drum, and a noise drum on the PSG.  A hit
is a few frames, below a MOD row, so it is rendered whole as a one-shot sample (docs/todo/
binary_import.md 3.5).  The reader runs the driver frame by frame; the frames are the chips'.

    FmDrum(voice, tl_offset, frames, psg)     frames[n]: what FM3 holds on V-int frame n of the hit
        FmFrame(word, keyed, attack)          word: block << 11 | fnum; attack: keyed on this frame
            slots, keys                       special mode: each operator's word, which are keyed
        PsgDrumFrame(divider, noise, ...)     psg[n]: what tone 3 and the noise channel hold
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chips import PSG_ATT_SILENT
from .song import SmpsVoice

ALL_OPERATORS = 0xF              # a key mask: bits 0-3 OP1-OP4 (register $28's bits 4-7)


@dataclass(frozen=True)
class FmFrame:
    word: int            # the frequency word written this frame (block << 11 | fnum)
    keyed: bool          # the channel is keyed on after this frame's writes
    attack: bool = False  # a key-on this frame (after a key-off when it was keyed)
    # Channel 3's special mode: each operator's word by register slot (+0 +4 +8 +C: OP1 OP3 OP2
    # OP4); None: every operator at `word`
    slots: tuple[int, ...] | None = None
    keys: int = ALL_OPERATORS    # the operators keyed while `keyed` (bits 0-3: OP1-OP4)


@dataclass(frozen=True)
class PsgDrumFrame:
    """Tone 3 and the noise channel at the end of a frame: the noise clocked by tone 3 (rate 3)."""

    divider: int         # tone 3's period
    noise: int           # the noise register byte (white / periodic, rate)
    tone_attenuation: int   # 15: silent
    noise_attenuation: int


@dataclass(frozen=True)
class FmDrum:
    voice: SmpsVoice
    tl_offset: int                 # the carrier TL the drum adds to its voice's (the drum's volume)
    frames: tuple[FmFrame, ...]    # from the hit to the program's stop (keyed off), or the read's cap
    cut: str = ""                  # why the frames end before a stop ("" when the program stopped)
    psg: tuple[PsgDrumFrame, ...] = ()   # the PSG part sounding with it, from the hit; () none

    @property
    def silent(self) -> bool:
        """Nothing keys on (Golden Axe's drum89: a rest): a hit only stops the drum before it."""
        return not any(f.attack for f in self.frames) and not any(_sounds(f) for f in self.psg)


def _sounds(frame: PsgDrumFrame) -> bool:
    return frame.tone_attenuation < PSG_ATT_SILENT or frame.noise_attenuation < PSG_ATT_SILENT
