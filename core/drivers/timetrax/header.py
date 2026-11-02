"""A Time Trax sound's header: a list of the tracks it starts (Z80 $08A2), each copied into the
track RAM of the channel it names.

    per track   priority.b  channel.b  address.w (big-endian)       ... then $FF
    channel     0-5 FM1-FM6, 6-7 the PSG's two tracks (tone 2, tone 3 / noise)

Every song plays FM1-FM5, two FM6 too; no song a PSG track (refused).  No tempo: a duration counts
frames.  The voices are the driver's, one table for every song (DriverTables.voices).
"""

from __future__ import annotations

from core.rom.grammar import track_label
from core.rom.header import RomHeader
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.smps import FM_CHANNEL_NAMES, NO_TEMPO_HOLDS, ChannelType, SmpsChannelHeader, SmpsSongHeader

from .memory import TimeTraxMemory

_END = 0xFF
_ENTRY = 4
_CHANNEL_AT, _ADDRESS_AT = 1, 2
_TRACKS = 8                           # the driver's track RAM: FM1-FM6, the PSG's two
_FM_TRACKS = len(FM_CHANNEL_NAMES)


def is_sound(memory: SoundMemory, address: int) -> bool:
    """Plausible as a sound header: 1-8 tracks, each on a channel of the driver's, each in the bank."""
    count = _entries(memory, address)
    if count is None:
        return False
    entries = [address + i * _ENTRY for i in range(count)]
    return all(memory.byte(at + _CHANNEL_AT) < _TRACKS and memory.contains(memory.header_pointer(address, at + _ADDRESS_AT))
               for at in entries)


def track_count(memory: SoundMemory, address: int) -> int:
    """The tracks a plausible header starts."""
    count = _entries(memory, address)
    assert count is not None
    return count


def read_header(memory: SoundMemory, address: int) -> RomHeader:
    """A song's header: its FM tracks, by channel."""
    if not isinstance(memory, TimeTraxMemory):
        raise TypeError("a Time Trax song reads through its driver's tables (TimeTraxMemory)")
    if not is_sound(memory, address):
        raise RomError(f"${address:X}: not a Time Trax sound header")

    voices = memory.tables.voices
    header = SmpsSongHeader(tempo_modifier=NO_TEMPO_HOLDS, voice_label=track_label(voices))
    tracks: dict[int, ChannelType] = {}
    for i in range(track_count(memory, address)):
        at = address + i * _ENTRY
        channel = memory.byte(at + _CHANNEL_AT)
        if channel >= _FM_TRACKS:
            raise RomError(f"${at:X}: a PSG track (channel {channel}): no song plays one, not read")
        start = memory.header_pointer(address, at + _ADDRESS_AT)
        if start in tracks:
            raise RomError(f"${at:X}: two tracks start at ${start:X}")
        tracks[start] = ChannelType.FM
        header.channels.append(SmpsChannelHeader(channel_type=ChannelType.FM, label=track_label(start),
                                                 chip_channel=FM_CHANNEL_NAMES[channel]))
    header.channels.sort(key=lambda c: FM_CHANNEL_NAMES.index(c.chip_channel))
    header.fm_count = len(header.channels)
    return RomHeader(header, voices, tracks)


def track_numbers(head: RomHeader) -> dict[int, int]:
    """Each track's start -> its track number (0-5: FM1-FM6), as $8D names it less one."""
    starts = {track_label(start): start for start in head.tracks}
    return {starts[c.label]: FM_CHANNEL_NAMES.index(c.chip_channel) for c in head.header.channels}


def _entries(memory: SoundMemory, address: int) -> int | None:
    """The entries before the $FF that ends them; None: no end within the driver's tracks."""
    for i in range(_TRACKS + 1):
        at = address + i * _ENTRY
        if not memory.contains(at):
            return None
        if memory.byte(at) == _END:
            return i or None
        if not memory.contains(at, _ENTRY):
            return None
    return None
