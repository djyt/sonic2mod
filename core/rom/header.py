"""Song and SFX headers: their fields, as every SMPS driver here lays them out.  Pointer values are
the driver's (SoundMemory.header_pointer: Sonic 1 relative to the header, ...); what each DAC / FM
entry plays on, the tempo byte that never stalls and the SFX channel ids are its HeaderLayout's.

    music   voices.w  fm.b psg.b  divider.b modifier.b
            DAC + FM tracks: ptr.w pitch.b volume.b            (Sonic 1: the first is the DAC;
                                                                Type 0 FM: drums, FM1 FM2 FM4 ...)
            PSG tracks:      ptr.w pitch.b volume.b mod.b envelope.b
    SFX     voices.w  divider.b count.b
            each track:      $80 channel.b ptr.w pitch.b volume.b
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import NO_TEMPO_HOLDS, SmpsChannelHeader, SmpsSongHeader, psg_voice_name
from .memory import SoundMemory
from .variant import HeaderLayout

_MUSIC_FIXED = 6         # voices, counts, tempo
_FM_TRACK = 4
_PSG_TRACK = 6
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
    tracks: dict[str, int]     # each track's label -> its first byte's address


def track_label(address: int) -> str:
    """The name a ROM song gives the code at `address` (a ROM has no labels)."""
    return f"loc_{address:05X}"


def read_music_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> RomHeader:
    if not is_music_header(memory, address, layout):
        raise ValueError(f"${address:X}: not a music header")

    fm_count, psg_count = memory.byte(address + 2), memory.byte(address + 3)
    tempo = memory.byte(address + 5)
    header = SmpsSongHeader(fm_count=fm_count, psg_count=psg_count, tempo_divider=memory.byte(address + 4),
                            tempo_modifier=NO_TEMPO_HOLDS if tempo == layout.never_holds else tempo,
                            tempo_phase=layout.tempo_phase)
    voices = _voices(memory, address)
    header.voice_label = track_label(voices) if voices is not None else ""
    tracks: dict[str, int] = {}

    # The DAC and FM tracks; the DAC's pitch and volume bytes go unread, as SmpsParser leaves them
    at = address + _MUSIC_FIXED
    for slot in layout.fm_slots[:fm_count]:
        start = memory.header_pointer(address, at)
        label = track_label(start)
        tracks[label] = start
        if slot.channel_type == "DAC":
            header.channels.append(SmpsChannelHeader(channel_type="DAC", label=label, chip_channel=slot.chip_channel))
        else:
            header.channels.append(SmpsChannelHeader(channel_type="FM", label=label, chip_channel=slot.chip_channel,
                                                     pitch_offset=_signed(memory.byte(at + 2)),
                                                     volume=memory.byte(at + 3)))
        at += _FM_TRACK

    for _ in range(psg_count):
        start = memory.header_pointer(address, at)
        label = track_label(start)
        tracks[label] = start
        header.channels.append(SmpsChannelHeader(channel_type="PSG", label=label,
                                                 pitch_offset=_signed(memory.byte(at + 2)),
                                                 volume=memory.byte(at + 3), mod_byte=memory.byte(at + 4),
                                                 psg_voice_label=psg_voice_name(memory.byte(at + 5))))
        at += _PSG_TRACK
    return RomHeader(header, voices, tracks)


def read_sfx_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> RomHeader:
    if not is_sfx_header(memory, address, layout):
        raise ValueError(f"${address:X}: not an SFX header")

    # SFX run one tick per V-int: no tempo modifier byte
    header = SmpsSongHeader(tempo_divider=memory.byte(address + 2), tempo_modifier=0, is_sfx=True)
    voices = _voices(memory, address)
    header.voice_label = track_label(voices) if voices is not None else ""
    tracks: dict[str, int] = {}

    at = address + _SFX_FIXED
    for _ in range(memory.byte(address + 3)):
        channel = memory.byte(at + 1)
        start = memory.header_pointer(address, at + 2)
        label = track_label(start)
        tracks[label] = start
        header.channels.append(SmpsChannelHeader(
            channel_type="PSG" if channel & _PSG_CHANNEL_BIT else "FM", label=label,
            pitch_offset=_signed(memory.byte(at + 4)), volume=memory.byte(at + 5), hw_channel=channel))
        at += _SFX_TRACK
    return RomHeader(header, voices, tracks)


def is_music_header(memory: SoundMemory, address: int, layout: HeaderLayout) -> bool:
    """Plausible as a music header: track counts the driver has RAM for, a tempo divider, and
    every pointer inside the ROM."""
    if not memory.contains(address, _MUSIC_FIXED):
        return False
    fm_count, psg_count = memory.byte(address + 2), memory.byte(address + 3)
    if not (1 <= fm_count <= len(layout.fm_slots) and psg_count <= _MAX_PSG_TRACKS and memory.byte(address + 4)):
        return False

    slots = [address + _MUSIC_FIXED + i * _FM_TRACK for i in range(fm_count)]
    slots += [address + _MUSIC_FIXED + fm_count * _FM_TRACK + i * _PSG_TRACK for i in range(psg_count)]
    if not memory.contains(address, (slots[-1] - address) + _PSG_TRACK):
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


def _voices(memory: SoundMemory, address: int) -> int | None:
    if not memory.word(address):
        return None
    return memory.header_pointer(address, address)


def _signed(byte: int) -> int:
    return byte - 0x100 if byte > 0x7F else byte
