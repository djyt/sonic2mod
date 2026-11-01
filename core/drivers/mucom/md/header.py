"""A Mucom-MD song: MUCOM88's compiled image, byte for byte ($40488).

    +0  $C200.w end.w   the PC-88's load address and the image's end (little-endian)
    +4  $00
    +5  voices.w        the voice bank, from +5
    +7  ?.w             not read by the 68k
    +9  $00             the base every track offset counts from
    +10 11 entries, MUCOM88's channels A-K: start.w loop.w (from +9; loop 0: none)

    A B C  FM1-FM3      D E F  PSG1-PSG3 (tones 1-3)      G  rhythm: never played
    H I    FM4 FM5      J      FM6: never played          K  ADPCM: the drum track (FM6 as DAC)

No tempo (a tick a frame), no volume or transposition per track.  A track's `$00` ends it, or
jumps to its loop (RomHeader.loops).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.rom.grammar import track_label
from core.rom.header import RomHeader
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.smps import NO_TEMPO_HOLDS, ChannelType, SmpsChannelHeader, SmpsSongHeader

_LOAD = 0xC200                  # the PC-88 address MUCOM88 compiles a song to
_ZERO_AT = 4
_VOICES_AT = 5
_BASE_AT = 9
_ENTRIES_AT = 10
_ENTRY = 4                      # start.w loop.w
_LOOP_AT = 2
_END = 0x00                     # a track's end


@dataclass(frozen=True)
class _Slot:
    channel_type: ChannelType
    chip_channel: str


# MUCOM88's channels A-K: the track each plays as, None for one the driver never plays
_CHANNELS: tuple[_Slot | None, ...] = (
    _Slot(ChannelType.FM, "FM1"), _Slot(ChannelType.FM, "FM2"), _Slot(ChannelType.FM, "FM3"),
    _Slot(ChannelType.PSG, "PSG1"), _Slot(ChannelType.PSG, "PSG2"), _Slot(ChannelType.PSG, "PSG3"),
    None,
    _Slot(ChannelType.FM, "FM4"), _Slot(ChannelType.FM, "FM5"),
    None,
    _Slot(ChannelType.DAC, ""),
)
_NAMES = "ABCDEFGHIJK"
_HEADER_SIZE = _ENTRIES_AT + len(_CHANNELS) * _ENTRY

# The order the song's channels are listed in: FM1-FM5, the drum track, the PSG (Streets of Rage's)
_ORDER = (ChannelType.FM, ChannelType.DAC, ChannelType.PSG)


def is_image(memory: SoundMemory, address: int) -> bool:
    """Plausible as a song image: MUCOM88's load address, its zero bytes, every track in the ROM."""
    if not memory.contains(address, _HEADER_SIZE):
        return False
    if memory.word(address) != _LOAD or memory.byte(address + _ZERO_AT):
        return False
    base = address + _BASE_AT
    return all(memory.contains(memory.header_pointer(base, address + _ENTRIES_AT + i * _ENTRY))
               for i in range(len(_CHANNELS)))


def read_image(memory: SoundMemory, address: int) -> RomHeader:
    """A song's image -> its header: FM1-FM5, the drum track and the PSG, each track's loop."""
    if not is_image(memory, address):
        raise RomError(f"${address:X}: not a MUCOM88 song image")

    base = address + _BASE_AT
    voices = memory.header_pointer(address + _VOICES_AT, address + _VOICES_AT)
    header = SmpsSongHeader(tempo_modifier=NO_TEMPO_HOLDS, voice_label=track_label(voices))
    tracks: dict[int, ChannelType] = {}
    loops: dict[int, int] = {}
    channels: list[SmpsChannelHeader] = []

    for i, slot in enumerate(_CHANNELS):
        at = address + _ENTRIES_AT + i * _ENTRY
        start = memory.header_pointer(base, at)
        empty = memory.byte(start) == _END

        # A channel the driver never plays must hold nothing
        if slot is None:
            if not empty:
                raise RomError(f"${at:X}: channel {_NAMES[i]} holds code, which the driver never plays")
            continue

        # An empty track stops: a loop of nothing would never end
        loop = memory.word(at + _LOOP_AT)
        if loop and not empty:
            loops[start] = base + loop
        tracks[start] = slot.channel_type
        channels.append(SmpsChannelHeader(channel_type=slot.channel_type, label=track_label(start),
                                          chip_channel=slot.chip_channel))

    header.channels = sorted(channels, key=lambda c: _ORDER.index(c.channel_type))
    header.fm_count = sum(c.channel_type != ChannelType.PSG for c in channels)
    header.psg_count = len(channels) - header.fm_count
    return RomHeader(header, voices, tracks, loops)
