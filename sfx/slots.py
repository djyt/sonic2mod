"""The SFX track slots an effect takes (Sound_PlaySFX): one per channel in its header, PSG3's tone
and noise sharing one.  A new effect cuts every track of an older one on a slot it takes, so a
mixer's voice allocation needs the set (Aonic's sfx.md §4.4).

    mask bit   0    1    2    3     4     5
    slot       FM3  FM4  FM5  PSG1  PSG2  PSG3 (tone or noise)
"""

from __future__ import annotations

from core.smps import SFX_CHANNEL_IDS

# VoiceControl byte -> name: 0x02 'FM3' ... 0xE0 'Noise'
_NAMES = {hw: name.removeprefix('c') for name, hw in SFX_CHANNEL_IDS.items()}

SLOTS = ("FM3", "FM4", "FM5", "PSG1", "PSG2", "PSG3")
_NOISE_SLOT = {"Noise": "PSG3"}


def channel_name(hw: int) -> str:
    """'FM5', 'Noise'; an unknown byte as '?XX'."""
    return _NAMES.get(hw, f"?{hw:02X}")


def slot_names(hws) -> list[str]:
    """The slots the channels take, in mask-bit order, each once."""
    taken = {_NOISE_SLOT.get(channel_name(hw), channel_name(hw)) for hw in hws}
    return [s for s in SLOTS if s in taken]


def slot_mask(hws) -> int:
    """The slots as a 6-bit mask (bit 0 FM3 ... bit 5 PSG3)."""
    return sum(1 << SLOTS.index(s) for s in slot_names(hws))
