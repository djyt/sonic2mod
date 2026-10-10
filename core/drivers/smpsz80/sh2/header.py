"""Space Harrier II's songs: no SMPS header but a track list, each record copied as it stands into
the driver's track RAM (Z80 $1839, a $30-byte slot each, in order; the SFX slots after).

    count.b, then per track 9 bytes:
      +0 flags          bit 7: plays; bit 6: pan animation (docs/todo/space_harrier_2.md 2.4)
      +1 channel        FM $00-$02, $04-$06; PSG $80 $A0 $C0
      +2 divider        durations x it (every track of a song alike)
      +3 pointer.w      a Z80 address, little-endian
      +5 transposition  signed
      +6 pitch envelope (2.3; 0: none)
      +7 voice          not loaded as the song starts: a track sets its own ($EF)
      +8 volume         added to the carriers' TL

The slot, not the channel byte, says how a track plays: slot 3 is the drum track on FM3 (Z80
$0CC0) and slot 7 its PSG half on PSG3 ($0DFE), which reads the drum track's own bytes (one
track in the song).  The tempo is not in the list: the driver's tempo table holds it, by song.

    Space Harrier II   every song FM1-FM6 and the drum track; all but $97 its PSG half too
"""

from __future__ import annotations

from core.rom.grammar import track_label
from core.rom.header import RomHeader
from core.rom.image import RomError
from core.rom.memory import SoundMemory
from core.rom.variant import HeaderLayout, TrackSlot
from core.smps import (
    FM_CHANNEL_NAMES,
    NO_TEMPO_HOLDS,
    PSG_CHANNEL_NAMES,
    ChannelType,
    SmpsChannelHeader,
    SmpsSongHeader,
    signed_byte,
)

from .memory import Sh2Memory

# The music slots: the drum track's on FM3, then its PSG half (read with the drum track)
HEADER_SH2 = HeaderLayout(
    fm_slots=(TrackSlot(ChannelType.FM, "FM1"), TrackSlot(ChannelType.FM, "FM2"), TrackSlot(ChannelType.DAC, "FM3"),
              TrackSlot(ChannelType.FM, "FM4"), TrackSlot(ChannelType.FM, "FM5"), TrackSlot(ChannelType.FM, "FM6")),
    sfx_channels=frozenset(),          # music only
    tempo=False,
)
_DRUM_PSG = "PSG3"
_MUSIC_SLOTS = len(HEADER_SH2.fm_slots) + 1

_RECORD = 9
_FLAGS, _CHANNEL, _DIVIDER, _POINTER, _TRANSPOSITION, _VOLUME = 0, 1, 2, 3, 5, 8
_PLAYS = 0x80
_NEVER_HOLDS = 0                       # tempo 0: the counter never runs out

# Each channel byte's chip channel: FM $00-$02 and $04-$06 (bit 2: the chip's second part), the
# PSG's tones $80 $A0 $C0
_CHANNELS = {**dict(zip((0x00, 0x01, 0x02, 0x04, 0x05, 0x06), FM_CHANNEL_NAMES, strict=True)),
             **dict(zip((0x80, 0xA0, 0xC0), PSG_CHANNEL_NAMES, strict=True))}


def is_track_list(memory: SoundMemory, address: int) -> bool:
    """Plausible as a track list: the driver's music slots or fewer, each record's channel a chip
    channel and its pointer in the bank."""
    if not memory.contains(address):
        return False
    count = memory.byte(address)
    if not (1 <= count <= _MUSIC_SLOTS and memory.contains(address, 1 + count * _RECORD)):
        return False
    records = [address + 1 + i * _RECORD for i in range(count)]
    return all(_chip_channel(memory.byte(at + _CHANNEL)) and memory.contains(memory.header_pointer(address, at + _POINTER))
               for at in records)


def read_track_list(memory: SoundMemory, address: int) -> RomHeader:
    """A song's track list -> its header: the drum track and FM1-FM6 (no PSG music)."""
    if not isinstance(memory, Sh2Memory):
        raise TypeError("a track list reads through the driver's tables (Sh2Memory)")
    if not is_track_list(memory, address):
        raise RomError(f"${address:X}: not a track list")

    records = [address + 1 + i * _RECORD for i in range(memory.byte(address))]
    dividers = {memory.byte(at + _DIVIDER) for at in records}
    if len(dividers) != 1:
        raise RomError(f"${address:X}: tracks at dividers {sorted(dividers)}: one per song read")
    tempo = memory.tempo(address)
    header = SmpsSongHeader(tempo_divider=dividers.pop(), tempo_modifier=NO_TEMPO_HOLDS if tempo == _NEVER_HOLDS else tempo)
    header.voice_label = track_label(memory.tables.voices)

    tracks: dict[int, ChannelType] = {}
    drums = None
    for slot, at in enumerate(records):
        start = memory.header_pointer(address, at + _POINTER)
        chip = _chip_channel(memory.byte(at + _CHANNEL))
        if slot == len(HEADER_SH2.fm_slots):
            if chip != _DRUM_PSG or start != drums:
                raise RomError(f"${at:X}: slot 7 on {chip} at ${start:X}: not the drum track's PSG half")
            continue

        expected = HEADER_SH2.fm_slots[slot]
        if chip != expected.chip_channel:
            raise RomError(f"${at:X}: slot {slot + 1} on {chip}, which the driver plays as {expected.chip_channel}")
        if expected.channel_type is ChannelType.DAC:
            drums = start
        if not memory.byte(at + _FLAGS) & _PLAYS:
            continue
        tracks[start] = expected.channel_type
        header.channels.append(SmpsChannelHeader(channel_type=expected.channel_type, label=track_label(start),
                                                 chip_channel=chip, pitch_offset=signed_byte(memory.byte(at + _TRANSPOSITION)),
                                                 volume=memory.byte(at + _VOLUME)))
    header.fm_count = len(header.channels)
    return RomHeader(header, memory.tables.voices, tracks)


def _chip_channel(byte: int) -> str:
    """"FM1".."FM6", "PSG1".."PSG3"; "" for a byte that names none (the noise channel too)."""
    return _CHANNELS.get(byte, "")
