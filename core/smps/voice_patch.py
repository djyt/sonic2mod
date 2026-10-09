"""A driver's register writes to a playing voice, as voices: Streets of Rage's `$FA r v`.

A track writes one of its voice's operator registers (a decay or release rate); the chip plays the
voice so changed until the next voice set rewrites every register.  This pass, run on the walked
song, gives each such voice its own copy (one per voice and set of patches, shared by every track
that plays it) and sets it where the write was, so everything after the walk sees voices only.

    SetVoice(3) ... VoiceRegister($6D, $10) ... VoiceRegister($7D, $12)
    SetVoice(3) ... SetVoice(10)               ... SetVoice(11)          (10, 11: voice 3 patched)

A write to a carrier's TL is refused (SmpsVoice.patched): the driver adds the header volume to it
and writes it again after each volume change, which no voice says (no song in the ROM does).
"""

from __future__ import annotations

from collections.abc import Iterator

from .effects import SetVoice, VoiceRegister
from .song import ChannelType, SmpsChannel, SmpsEvent, SmpsSong, SmpsVoice

_FM_PART = 3                    # channels per YM2612 part: a register's low bits name one of them


def apply_voice_patches(song: SmpsSong) -> None:
    """Every FM track's register writes as patched voices, set where each was written."""
    patcher = _Patcher(song.voices)
    for channel in song.channels:
        if any(isinstance(ev.effect, VoiceRegister) for ev in channel.events):
            channel.events = list(patcher.events(channel))
    song.voices = [*song.voices, *patcher.added]


class _Patcher:
    """The patched copies of a song's voices, one per voice and set of patches."""

    def __init__(self, voices: list[SmpsVoice]):
        self._voices = {v.index: v for v in voices}
        self._copies: dict[tuple[int, tuple[tuple[int, int], ...]], int] = {}
        self.added: list[SmpsVoice] = []

    def events(self, channel: SmpsChannel) -> Iterator[SmpsEvent]:
        """`channel`'s events, each register write a SetVoice of the voice it leaves."""
        base: int | None = None
        patches: dict[int, int] = {}
        for ev in channel.events:
            match ev.effect:
                case SetVoice(index=index):
                    base, patches = index, {}
                case VoiceRegister(register=register, value=value):
                    if base is None or channel.header.channel_type != ChannelType.FM:
                        raise ValueError(f"{channel.header.label}: a register write with no FM voice set")
                    patches[register - _channel_number(channel)] = value
                    yield SmpsEvent(effect=SetVoice(self._copy(base, patches)), tick_position=ev.tick_position)
                    continue
            yield ev

    def _copy(self, base: int, patches: dict[int, int]) -> int:
        """The index of voice `base` with `patches` (register: byte) written over it."""
        key = (base, tuple(sorted(patches.items())))
        if key not in self._copies:
            voice = self._voices[base]
            for register, value in patches.items():
                voice = voice.patched(register, value)
            voice.index = max(self._voices) + 1
            self._voices[voice.index] = voice
            self._copies[key] = voice.index
            self.added.append(voice)
        return self._copies[key]


def _channel_number(channel: SmpsChannel) -> int:
    """The track's channel within its YM2612 part: what its register writes carry in their low bits."""
    return (int(channel.header.chip_channel.removeprefix("FM")) - 1) % _FM_PART
