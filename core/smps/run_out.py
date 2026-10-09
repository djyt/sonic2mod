"""A driver's key-on run-out: a note held this many frames without an attacking read is keyed off.

Type 0 FM's fill counter (Golden Axe, Z80 $00E8) is reset by every read that attacks and counts
down on every frame that reads nothing, TempoWait's holds included; at 256 it keys the channel off.
A tie (smpsNoAttack) neither resets nor counts, and one read after the key-off does not key on
again.  The rips agree: long FM1 notes in Death Adder, The Battle and Conclusion end 258-260
frames after their attack, and the 8 ties each of The Battle and Conclusion read after it are
silent.  Sonic 1 has no run-out.

The song's driver states the limit (PlaybackRules.key_run_out); this pass, run on the walked song, cuts
each FM note that outlasts it and makes the rest of what it held a rest.  Every pass after it -
playback, the yardstick, the sample lengths, the converter's release - sees the key-off.  It
runs before the loops are replayed and the global tempo divider applied: a replayed loop keeps
the first pass's cuts, and a tie chain across the jump is counted from neither side.

    attack  tie   tie                   frames: the attack's read, then 256 counted (a tie's read
    |-------|-----|--------------|      frame is not counted)
                      ^ key-off: the note cut here, the rest of the chain a rest
"""

from __future__ import annotations

import dataclasses

from .song import REST, ChannelType, SmpsEvent, SmpsNote, SmpsSong
from .tempo import TempoSegment, frame_of_tick, tick_at_frame


def apply_run_out(song: SmpsSong) -> None:
    """Every FM channel's notes cut where the driver's run-out keys them off (none without one)."""
    limit = song.rules.key_run_out
    if not limit:
        return
    schedule = song.tempo_schedule()
    for channel in song.channels:
        if channel.header.channel_type == ChannelType.FM:
            channel.events = _cut(channel.events, schedule, limit)


def _cut(events: list[SmpsEvent], schedule: tuple[TempoSegment, ...], limit: int) -> list[SmpsEvent]:
    out: list[SmpsEvent] = []
    key_off: int | None = None          # the frame the held note is keyed off on; None: no note held
    for ev in events:
        note = ev.note
        if note is None:
            out.append(ev)
            continue
        if note.is_rest:
            key_off = None
            out.append(ev)
            continue

        # An attack starts the count; a tie read before the key-off is a frame the count skips
        read = frame_of_tick(schedule, ev.tick_position)
        if not note.is_no_attack:
            key_off = read + limit
        elif key_off is not None and read <= key_off:
            key_off += 1

        off_tick = None if key_off is None else tick_at_frame(schedule, key_off)
        if off_tick is None or ev.tick_position + note.duration <= off_tick:
            out.append(ev)
            continue

        # Keyed off before or inside this note: what is left of it is a rest
        held = max(0, off_tick - ev.tick_position)
        if held:
            out.append(SmpsEvent(note=_with(note, held), tick_position=ev.tick_position))
        out.append(SmpsEvent(note=SmpsNote(REST, note.duration - held, is_rest=True, cut=True),
                             tick_position=ev.tick_position + held))
    return out


def _with(note: SmpsNote, duration: int) -> SmpsNote:
    """`note` cut to `duration`."""
    return dataclasses.replace(note, duration=duration, cut=True)
