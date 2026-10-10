"""The hardware LFO: each FM note's voice a copy under the LFO it plays with.

The chip has one LFO; $22 sets its frequency for every channel, and a channel's B4 says how far
it moves that channel (FMS: pitch, AMS: the level of operators with AM on).  Streets of Rage's
`$FC f p a` writes both at once, so the frequency a note plays at is the last write by any track:

    FM2   $FC 4 6 0 ........ C4 ........ C4 ........ $FC 0 0 0
    FM5   ............ $FC 2 3 2 ..................
                               ^ FM2's second C4 plays at frequency 2 (FMS 6)

This pass, run on the walked song, gives every attacking FM note whose track has a sensitivity
the voice it plays as a copy under its LFO (VoicePatcher.under_lfo), a SetVoice before the note
where that is not the voice already set.  Everything after it sees voices only.  It runs before
the loops are replayed: a replay keeps the first pass's frequencies, and each loop's first note
sets its voice again (the end of a pass may leave another one set).  Within a frame the driver
updates the tracks in header order, so a later track's write at a note's frame comes after it.
"""

from __future__ import annotations

import bisect

from ..chips import FmLfo
from .effects import Lfo, SetVoice
from .song import ChannelType, SmpsChannel, SmpsEvent
from .voice_patch import VoicePatcher

_At = tuple[int, int]             # (tick, the track's place in the header): when, within a frame


def apply_lfo(channels: list[SmpsChannel], patcher: VoicePatcher) -> None:
    """Each FM channel's attacking notes set to the voice copy under the LFO they play with."""
    writes = sorted(((ev.tick_position, order), ev.effect.frequency) for order, ch in enumerate(channels)
                    for ev in ch.events if isinstance(ev.effect, Lfo))
    if not writes:
        return
    frequencies = _Frequencies(writes)
    for order, channel in enumerate(channels):
        if channel.header.channel_type == ChannelType.FM:
            _set_voices(channel, order, frequencies, patcher)


class _Frequencies:
    """The chip's LFO frequency by time: the last write at or before it."""

    def __init__(self, writes: list[tuple[_At, int]]):
        self._at = [at for at, _ in writes]
        self._frequency = [f for _, f in writes]

    def at(self, when: _At) -> int | None:
        i = bisect.bisect_right(self._at, when)
        return self._frequency[i - 1] if i else None


def _set_voices(channel: SmpsChannel, order: int, frequencies: _Frequencies, patcher: VoicePatcher) -> None:
    """`channel`'s events with a SetVoice before each attacking note whose voice under its LFO
    is not the one set (and before the loop's first note); its loop index kept on its event."""
    first_in_loop = _first_attack(channel.events, channel.loop_event_index)
    voice: int | None = None           # the voice the walk set
    sensitivity = (0, 0)               # (FMS, AMS)
    playing: int | None = None         # the voice the chip plays: the walk's, or one set here
    out: list[SmpsEvent] = []
    loop_at = channel.loop_event_index
    for i, ev in enumerate(channel.events):
        if i == channel.loop_event_index:
            loop_at = len(out)
        match ev.effect:
            case SetVoice(index=index):
                voice = playing = index
            case Lfo(fms=fms, ams=ams):
                sensitivity = (fms, ams)
        note = ev.note
        if note is not None and not note.is_rest and not note.is_no_attack and voice is not None:
            wanted = patcher.under_lfo(voice, _lfo(frequencies.at((ev.tick_position, order)), *sensitivity))
            if wanted != playing or i == first_in_loop:
                out.append(SmpsEvent(effect=SetVoice(wanted), tick_position=ev.tick_position))
                playing = wanted
        out.append(ev)
    channel.events = out
    channel.loop_event_index = loop_at


def _lfo(frequency: int | None, fms: int, ams: int) -> FmLfo | None:
    """What the LFO does to a note: nothing before any write or at no sensitivity."""
    if frequency is None or not (fms or ams):
        return None
    return FmLfo(frequency, fms, ams)


def _first_attack(events: list[SmpsEvent], start: int | None) -> int | None:
    """The index of the first attacking note from `start` on (None: no loop, or none)."""
    if start is None:
        return None
    return next((i for i in range(start, len(events))
                 if (n := events[i].note) is not None and not n.is_rest and not n.is_no_attack), None)
