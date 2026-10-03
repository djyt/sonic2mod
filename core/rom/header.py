"""Song and SFX headers as the Sonic 1 driver reads them (SonicDriverVer 1: pointers relative to
the header's own address).

    music   voices.w  fm.b psg.b  divider.b modifier.b
            DAC + FM tracks: ptr.w pitch.b volume.b            (the first is the DAC)
            PSG tracks:      ptr.w pitch.b volume.b mod.b envelope.b
    SFX     voices.w  divider.b count.b
            each track:      $80 channel.b ptr.w pitch.b volume.b
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import SFX_CHANNEL_IDS, SmpsChannelHeader, SmpsSongHeader, psg_voice_name

_MUSIC_FIXED = 6         # voices, counts, tempo
_FM_TRACK = 4
_PSG_TRACK = 6
_SFX_FIXED = 4           # voices, divider, count
_SFX_TRACK = 6
_SFX_TRACK_MARK = 0x80

# The driver's track RAM: the DAC + FM1-FM6 tracks, PSG1-PSG3
_MAX_FM_TRACKS = 7
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


def read_music_header(rom, address: int) -> RomHeader:
    if not is_music_header(rom, address):
        raise ValueError(f"${address:X}: not a music header")

    fm_count, psg_count = rom.byte(address + 2), rom.byte(address + 3)
    header = SmpsSongHeader(fm_count=fm_count, psg_count=psg_count,
                            tempo_divider=rom.byte(address + 4), tempo_modifier=rom.byte(address + 5))
    voices = _voices(rom, address)
    header.voice_label = track_label(voices) if voices is not None else ""
    tracks: dict[str, int] = {}

    # The DAC and FM tracks; the DAC's pitch and volume bytes go unread, as SmpsParser leaves them
    at = address + _MUSIC_FIXED
    for i in range(fm_count):
        start = address + rom.word(at)
        label = track_label(start)
        tracks[label] = start
        if i == 0:
            header.channels.append(SmpsChannelHeader(channel_type="DAC", label=label))
        else:
            header.channels.append(SmpsChannelHeader(channel_type="FM", label=label,
                                                     pitch_offset=_signed(rom.byte(at + 2)),
                                                     volume=rom.byte(at + 3)))
        at += _FM_TRACK

    for _ in range(psg_count):
        start = address + rom.word(at)
        label = track_label(start)
        tracks[label] = start
        header.channels.append(SmpsChannelHeader(channel_type="PSG", label=label,
                                                 pitch_offset=_signed(rom.byte(at + 2)),
                                                 volume=rom.byte(at + 3), mod_byte=rom.byte(at + 4),
                                                 psg_voice_label=psg_voice_name(rom.byte(at + 5))))
        at += _PSG_TRACK
    return RomHeader(header, voices, tracks)


def read_sfx_header(rom, address: int) -> RomHeader:
    if not is_sfx_header(rom, address):
        raise ValueError(f"${address:X}: not an SFX header")

    # SFX run one tick per V-int: no tempo modifier byte
    header = SmpsSongHeader(tempo_divider=rom.byte(address + 2), tempo_modifier=0, is_sfx=True)
    voices = _voices(rom, address)
    header.voice_label = track_label(voices) if voices is not None else ""
    tracks: dict[str, int] = {}

    at = address + _SFX_FIXED
    for _ in range(rom.byte(address + 3)):
        channel = rom.byte(at + 1)
        start = address + rom.word(at + 2)
        label = track_label(start)
        tracks[label] = start
        header.channels.append(SmpsChannelHeader(
            channel_type="PSG" if channel & _PSG_CHANNEL_BIT else "FM", label=label,
            pitch_offset=_signed(rom.byte(at + 4)), volume=rom.byte(at + 5), hw_channel=channel))
        at += _SFX_TRACK
    return RomHeader(header, voices, tracks)


def is_music_header(rom, address: int) -> bool:
    """Plausible as a music header: track counts the driver has RAM for, a tempo, and every
    pointer inside the ROM."""
    if not rom.contains(address, _MUSIC_FIXED):
        return False
    fm_count, psg_count = rom.byte(address + 2), rom.byte(address + 3)
    if not (1 <= fm_count <= _MAX_FM_TRACKS and psg_count <= _MAX_PSG_TRACKS and rom.byte(address + 4)):
        return False

    slots = [address + _MUSIC_FIXED + i * _FM_TRACK for i in range(fm_count)]
    slots += [address + _MUSIC_FIXED + fm_count * _FM_TRACK + i * _PSG_TRACK for i in range(psg_count)]
    if not rom.contains(address, (slots[-1] - address) + _PSG_TRACK):
        return False
    return all(rom.contains(address + rom.word(slot)) for slot in slots)


def is_sfx_header(rom, address: int) -> bool:
    """Plausible as an SFX header: every track entry marked $80 with a known channel id."""
    if not rom.contains(address, _SFX_FIXED):
        return False
    count = rom.byte(address + 3)
    if not (1 <= count <= _MAX_SFX_TRACKS and rom.contains(address, _SFX_FIXED + count * _SFX_TRACK)):
        return False

    entries = [address + _SFX_FIXED + i * _SFX_TRACK for i in range(count)]
    return all(rom.byte(at) == _SFX_TRACK_MARK and rom.byte(at + 1) in SFX_CHANNEL_IDS.values()
               and rom.contains(address + rom.word(at + 2)) for at in entries)


def _voices(rom, address: int) -> int | None:
    offset = rom.word(address)
    return address + offset if offset else None


def _signed(byte: int) -> int:
    return byte - 0x100 if byte > 0x7F else byte
