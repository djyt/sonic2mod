"""A frame log lifted back into the song the driver played: FrameLog -> SmpsSong.

The SMPS driver is deterministic, so each write pattern maps back to the flag that produced it
(frequency table -> note byte, TempoWait hold frames -> ticks, PSG envelope curves ->
smpsPSGvoice, ...).  Everything after the parser runs unchanged on the result.

    frames ──tracks.py──► each track's hits by frame ──tempo.py──► ticks ──► SmpsSong
                          (FM key writes, PSG periods,           (+ tempo changes,
                           DAC seeks)                              the rip's loop)

A rip starts where its recording does: tick 0 is the first frame of the hold cycle the first
note plays in, and a rip's loop is wherever its ripper put it (every channel loops there).
Plan and status: docs/todo/vgz_conversion.md, Phase 1.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from ...smps import (
    DEFAULT_DRIVER,
    CoordFlag,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsDriver,
    SmpsEffect,
    SmpsEvent,
    SmpsNote,
    SmpsSong,
    SmpsSongHeader,
)
from ..chipstate import PSG_TONE_CHANNELS
from ..frames import FrameLog
from ..reader import VgmError
from .tempo import TempoError, TempoMap, infer_tempo
from .tracks import Hit, dac_hits, dac_used, fm_hits, fm_key_on_frames, noise_mode, psg_hits, psg_period_frames

_DAC_FM_CHANNELS = 5            # with the DAC on, FM6 is the DAC


class VgmLiftError(VgmError):
    """The log is not one the driver could have written, or the lift cannot read it yet."""


@dataclass(frozen=True)
class LiftOptions:
    """What the log cannot say for itself (a config's driver: / tempo_modifier: / tempo_divider:)."""

    driver: SmpsDriver = DEFAULT_DRIVER
    tempo_modifier: int | None = None     # None: inferred from the frames TempoWait holds
    tempo_divider: int | None = None      # None: the grid the FM notes start on (a divider is only spelling)


def lift_song(frames: FrameLog, options: LiftOptions | None = None) -> SmpsSong:
    """The song `frames` are a recording of, as SmpsParser would have read it from the asm."""
    options = options or LiftOptions()
    if options.driver != DEFAULT_DRIVER:
        raise VgmLiftError(f"driver: {options.driver}: the lift reads {DEFAULT_DRIVER} logs only")

    # Each track's hits by frame, in the order the asm declares its tracks
    tracks = _tracks(frames)
    if not any(hits for _, hits in tracks):
        raise VgmLiftError("the log plays no note")

    # The tempo: from the writes that land on ticks
    key_writes = {f"FM{ch + 1}": fs for ch, fs in fm_key_on_frames(frames)}
    key_writes |= {f"PSG{ch + 1}": fs for ch in range(PSG_TONE_CHANNELS) if (fs := psg_period_frames(frames, ch))}
    first = min(h.frame for _, hits in tracks for h in hits if not h.rest)       # the first note: no later than tick 0
    try:
        tempo = infer_tempo(key_writes, first, options.tempo_modifier)
    except TempoError as e:
        raise VgmLiftError(str(e)) from e

    # The song: each track's hits at their ticks, one pass to the log's end.  The loop and the
    # end are where the log jumps from and to: the frames whose bursts follow them.
    grid = _onset_grid(tracks, tempo)
    end = tempo.tick(frames.burst_frame(frames.end_sample) + 1)
    loop = None if frames.loop_sample is None else tempo.tick(frames.burst_frame(frames.loop_sample) + 1)
    if loop is not None:
        end = _whole_loop(loop, end, grid)
    channels = [_channel(header, hits, tempo, end) for header, hits in tracks]
    _mark_tempo_changes(channels, tempo)
    if loop is not None:
        _mark_loop(channels, loop)

    header = SmpsSongHeader(
        fm_count=sum(c.header.channel_type != "PSG" for c in channels),
        psg_count=sum(c.header.channel_type == "PSG" for c in channels),
        tempo_divider=options.tempo_divider or grid,
        tempo_modifier=tempo.modifier,
        channels=[c.header for c in channels])
    return SmpsSong(header, channels)


def _tracks(fl: FrameLog) -> list[tuple[SmpsChannelHeader, list[Hit]]]:
    """(header, hits) of every track: the DAC, FM1 up to the last FM channel used, PSG1-3."""
    tracks: list[tuple[SmpsChannelHeader, list[Hit]]] = []
    fm_channels = _DAC_FM_CHANNELS if dac_used(fl) else _DAC_FM_CHANNELS + 1
    if dac_used(fl):
        tracks.append((SmpsChannelHeader("DAC", "DAC"), dac_hits(fl)))

    fm = [fm_hits(fl, ch) for ch in range(fm_channels)]
    used = max((ch + 1 for ch, hits in enumerate(fm) if hits), default=0)
    tracks += [(SmpsChannelHeader("FM", f"FM{ch + 1}"), fm[ch]) for ch in range(used)]

    noise = noise_mode(fl)
    for ch in range(PSG_TONE_CHANNELS):
        last = ch == PSG_TONE_CHANNELS - 1
        tracks.append((SmpsChannelHeader("PSG", f"PSG{ch + 1}"), psg_hits(fl, ch, noise and last)))
    return tracks


def _channel(header: SmpsChannelHeader, hits: list[Hit], tempo: TempoMap, end: int) -> SmpsChannel:
    """A track's hits as events: each lasts to the next, a rest before the first."""
    at: dict[int, Hit] = {}
    for hit in hits:
        at[tempo.tick(hit.frame)] = hit           # one read a tick: the later write wins
    ticks = sorted(t for t in at if t < end)

    events = []
    if not ticks or ticks[0] > 0:
        events.append(SmpsEvent(_rest(ticks[0] if ticks else end), tick_position=0))
    for tick, following in itertools.pairwise([*ticks, end]):
        hit = at[tick]
        note = SmpsNote(hit.note, following - tick, is_rest=hit.rest, is_dac=bool(hit.dac), dac_name=hit.dac,
                        is_no_attack=not hit.attack)
        events.append(SmpsEvent(note, tick_position=tick))
    return SmpsChannel(header, events)


def _rest(duration: int) -> SmpsNote:
    return SmpsNote(Hit(0).note, duration, is_rest=True)


def _split(channel: SmpsChannel, tick: int) -> int:
    """The index of the first event at `tick` or later, a note spanning it split there (a tie,
    or two rests)."""
    for i, ev in enumerate(channel.events):
        if ev.tick_position >= tick:
            return i
        if ev.note is None or ev.tick_position + ev.note.duration <= tick:
            continue
        before = tick - ev.tick_position
        rest = SmpsNote(**{**vars(ev.note), "duration": ev.note.duration - before, "is_no_attack": not ev.note.is_rest})
        ev.note = SmpsNote(**{**vars(ev.note), "duration": before})
        channel.events.insert(i + 1, SmpsEvent(rest, tick_position=tick))
        return i + 1
    return len(channel.events)


def _mark_tempo_changes(channels: list[SmpsChannel], tempo: TempoMap) -> None:
    """smpsSetTempoMod where each change was read: on the first FM track (the Sonic 1 songs read
    them there)."""
    carrier = next((c for c in channels if c.header.channel_type == "FM"), channels[0])
    for tick, modifier in tempo.changes():
        i = _split(carrier, tick)
        carrier.events.insert(i, SmpsEvent(effect=SmpsEffect(CoordFlag.SET_TEMPO_MOD, [modifier]), tick_position=tick))


def _mark_loop(channels: list[SmpsChannel], tick: int) -> None:
    """Every track jumps back to `tick` from the end: where the rip loops."""
    for channel in channels:
        channel.has_jump = True
        channel.loop_tick = tick
        channel.loop_event_index = _split(channel, tick)


def _onset_grid(tracks: list[tuple[SmpsChannelHeader, list[Hit]]], tempo: TempoMap) -> int:
    """The longest length every interval between an FM track's notes is a multiple of (FM key-ons
    are the hits that land exactly on ticks)."""
    steps = []
    for header, hits in tracks:
        if header.channel_type != "FM":
            continue
        ticks = sorted({tempo.tick(h.frame) for h in hits if not h.rest})
        steps += [b - a for a, b in itertools.pairwise(ticks)]
    return math.gcd(*steps) or 1


def _whole_loop(loop: int, end: int, grid: int) -> int:
    """The end of a loop of whole grid steps.  A rip's loop can be a frame longer than the song's
    (Final Zone: 1153 frames for 960 ticks' 1152 - a V-int lost in it, or the ripper's choice): a
    span one tick off the notes' grid is one frame off it."""
    span = end - loop
    whole = round(span / grid) * grid
    return loop + whole if abs(whole - span) <= 1 else end
