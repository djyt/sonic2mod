"""Song and SFX headers: their fields, as each driver's HeaderLayout lays them out.  Pointer values
are the driver's (SoundMemory.header_pointer: Sonic 1 relative to the header, ...).

    music   voices.w  fm.b psg.b  [divider.b modifier.b]        (HeaderLayout.tempo)
            DAC + FM tracks: ptr.w ...  (fm_entry: SMPS pitch.b volume.b; Sonic 1: the first is the
                                         DAC; Type 0 FM: drums, FM1 FM2 FM4 ...)
            PSG tracks:      ptr.w ...  (psg_entry: SMPS pitch.b volume.b mod.b envelope.b)
    SFX     voices.w  divider.b count.b
            each track:      $80 channel.b ptr.w pitch.b volume.b

A driver whose headers are laid out otherwise reads them itself (SmpsVariant.music_header_reader,
sfx_header_reader); music_header / sfx_header ask it first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..smps import NO_TEMPO_HOLDS, ChannelType, SmpsChannelHeader, SmpsSongHeader, psg_voice_name, signed_byte
from .grammar import track_label
from .image import RomError
from .memory import SoundMemory
from .variant import HeaderLayout, SmpsVariant

_FM_COUNT_AT, _PSG_COUNT_AT = 2, 3
_DIVIDER_AT, _MODIFIER_AT = 4, 5
_SFX_FIXED = 4           # voices, divider, count
_SFX_TRACK = 6
_SFX_TRACK_MARK = 0x80

# The driver's track RAM: PSG1-PSG3, the SFX tracks (the DAC / FM tracks: HeaderLayout.fm_slots)
_MAX_PSG_TRACKS = 3
_MAX_SFX_TRACKS = 6

_PSG_CHANNEL_BIT = 0x80  # an SFX channel id with bit 7 set is a PSG channel


@dataclass(frozen=True)
class RomHeader:
    header: SmpsSongHeader
    voices: int | None         # the voice bank's address; None: the song has none (smpsHeaderVoiceNull)
    tracks: dict[int, ChannelType]     # each track's first byte -> the kind of track it starts


_Plausible = Callable[[SoundMemory, int], bool]


def read_index(memory: SoundMemory, slots: range, pointer: Callable[[int], int], first_id: int,
               plausible: _Plausible) -> dict[int, int]:
    """An index's entries by sound ID: the header each slot points at, up to the first slot that
    points at none."""
    entries: dict[int, int] = {}
    for at in slots:
        address = pointer(at)
        if not _points_at(memory, address, plausible):
            break
        entries[first_id + len(entries)] = address
    if not entries:
        raise RomError(f"the index at ${slots.start:X} points at no header")
    return entries


def is_index(memory: SoundMemory, slots: range, pointer: Callable[[int], int], plausible: _Plausible) -> bool:
    """Every slot points at a plausible header: a table's first entries, when locating an index."""
    return all(_points_at(memory, pointer(at), plausible) for at in slots)


def _points_at(memory: SoundMemory, address: int, plausible: _Plausible) -> bool:
    return memory.contains(address) and plausible(memory, address)


def music_header(memory: SoundMemory, address: int, variant: SmpsVariant) -> RomHeader:
    """A song's header: its driver's own reader's where it has one, else SMPS's."""
    if variant.music_header_reader:
        return variant.music_header_reader(memory, address)
    return read_music_header(memory, address, variant.header)


def sfx_header(memory: SoundMemory, address: int, variant: SmpsVariant) -> RomHeader:
    """An SFX's header: its driver's own reader's where it has one, else SMPS's."""
    if variant.sfx_header_reader:
        return variant.sfx_header_reader(memory, address)
    return read_sfx_header(memory, address, variant.header)


def read_music_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> RomHeader:
    if not is_music_header(memory, address, layout):
        raise RomError(f"${address:X}: not a music header")

    fm_count, psg_count = memory.byte(address + _FM_COUNT_AT), memory.byte(address + _PSG_COUNT_AT)
    header = SmpsSongHeader(fm_count=fm_count, psg_count=psg_count)
    if layout.tempo:
        tempo = memory.byte(address + _MODIFIER_AT)
        header.tempo_divider = memory.byte(address + _DIVIDER_AT)
        header.tempo_modifier = NO_TEMPO_HOLDS if tempo == layout.never_holds else tempo
    else:
        header.tempo_modifier = NO_TEMPO_HOLDS            # a tick every frame
    voices = _voices(memory, address)
    header.voice_label = track_label(voices) if voices is not None else ""
    tracks: dict[int, ChannelType] = {}

    # The DAC and FM tracks; the DAC's pitch and volume bytes go unread, as SmpsParser leaves them
    at = address + layout.entries_at
    fm, psg = layout.fm_entry, layout.psg_entry
    for slot in layout.fm_slots[:fm_count]:
        start = memory.header_pointer(address, at)
        label = track_label(start)
        tracks[start] = slot.channel_type
        if slot.channel_type == ChannelType.DAC:
            header.channels.append(SmpsChannelHeader(channel_type=ChannelType.DAC, label=label, chip_channel=slot.chip_channel))
        else:
            header.channels.append(SmpsChannelHeader(channel_type=ChannelType.FM, label=label, chip_channel=slot.chip_channel,
                                                     pitch_offset=signed_byte(_field(memory, at, fm.pitch)),
                                                     volume=_field(memory, at, fm.volume)))
        at += fm.size

    for i in range(psg_count):
        start = memory.header_pointer(address, at)
        label = track_label(start)
        tracks[start] = ChannelType.PSG
        chip = layout.psg_slots[i] if layout.psg_slots else ""
        header.channels.append(SmpsChannelHeader(channel_type=ChannelType.PSG, label=label, chip_channel=chip,
                                                 pitch_offset=signed_byte(_field(memory, at, psg.pitch)),
                                                 volume=_field(memory, at, psg.volume),
                                                 mod_byte=_field(memory, at, psg.mod),
                                                 psg_voice_label=psg_voice_name(_field(memory, at, psg.envelope))))
        at += psg.size
    return RomHeader(header, voices, tracks)


def read_sfx_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> RomHeader:
    if not layout.sfx_channels:
        raise RomError(f"${address:X}: SFX not read for this driver (music only)")
    if not is_sfx_header(memory, address, layout):
        raise RomError(f"${address:X}: not an SFX header")

    # SFX run one tick per V-int: no tempo modifier byte
    header = SmpsSongHeader(tempo_divider=memory.byte(address + 2), tempo_modifier=0, is_sfx=True)
    voices = _voices(memory, address)
    header.voice_label = track_label(voices) if voices is not None else ""
    tracks: dict[int, ChannelType] = {}

    at = address + _SFX_FIXED
    for _ in range(memory.byte(address + 3)):
        channel = memory.byte(at + 1)
        start = memory.header_pointer(address, at + 2)
        label = track_label(start)
        kind = ChannelType.PSG if channel & _PSG_CHANNEL_BIT else ChannelType.FM
        tracks[start] = kind
        header.channels.append(SmpsChannelHeader(
            channel_type=kind, label=label,
            pitch_offset=signed_byte(memory.byte(at + 4)), volume=memory.byte(at + 5), hw_channel=channel))
        at += _SFX_TRACK
    return RomHeader(header, voices, tracks)


def is_music_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> bool:
    """Plausible as a music header: track counts the driver has RAM for, a tempo divider (where it
    has one), and every pointer inside the ROM."""
    if not memory.contains(address, layout.entries_at):
        return False
    fm_count, psg_count = memory.byte(address + _FM_COUNT_AT), memory.byte(address + _PSG_COUNT_AT)
    if not (1 <= fm_count <= len(layout.fm_slots) and psg_count <= _MAX_PSG_TRACKS):
        return False
    if layout.tempo and not memory.byte(address + _DIVIDER_AT):
        return False

    fm, psg = layout.fm_entry, layout.psg_entry
    first = address + layout.entries_at
    slots = [first + i * fm.size for i in range(fm_count)]
    slots += [first + fm_count * fm.size + i * psg.size for i in range(psg_count)]
    last = psg.size if psg_count else fm.size
    if not memory.contains(address, (slots[-1] - address) + last):
        return False
    return all(memory.contains(memory.header_pointer(address, slot)) for slot in slots)


def is_sfx_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> bool:
    """Plausible as an SFX header: every track entry marked $80 with one of the driver's channel
    ids (Sonic 1's: FM3-FM5, the PSG; Type 0 FM's FM6 too)."""
    if not memory.contains(address, _SFX_FIXED):
        return False
    count = memory.byte(address + 3)
    if not (1 <= count <= _MAX_SFX_TRACKS and memory.contains(address, _SFX_FIXED + count * _SFX_TRACK)):
        return False

    entries = [address + _SFX_FIXED + i * _SFX_TRACK for i in range(count)]
    return all(memory.byte(at) == _SFX_TRACK_MARK and memory.byte(at + 1) in layout.sfx_channels
               and memory.contains(memory.header_pointer(address, at + 2)) for at in entries)


def _field(memory: SoundMemory, entry: int, offset: int | None) -> int:
    """An entry's byte at `offset`; 0 where the driver stores none."""
    return 0 if offset is None else memory.byte(entry + offset)


def _voices(memory: SoundMemory, address: int) -> int | None:
    if not memory.word(address):
        return None
    return memory.header_pointer(address, address)


