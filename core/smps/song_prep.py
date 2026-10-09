"""The parsed song re-timed and unrolled the way the driver plays it, before anything counts notes.

    song ──► prepare_song ──► PreparedSong: a new song, and what changed
                 _apply_global_tempo_div    smpsSetTempoDiv re-times every track
                 _extend_looping_channels   a short jump loop replayed to the song's end: the
                                            last jump target + the loops' common period

Tracks may loop at different lengths (Streets of Rage $8F: 2304, 1728 and 4608 frames): the song
repeats only after their least common multiple (13824), so each is replayed to the latest loop
start plus that period, and the MOD's loop goes back there.  A loop's period is the shortest that
its body repeats at (Green Hill's drum loop: 1024 ticks, a 512-tick bar twice, so 1536 holds it).
A loop under 1 / _MAX_LOOP_SPANS
of the longest is a texture, not a part (Green Hill's 8-tick hi-hat): it is replayed to the end
but sets no period.  A period past _MAX_LOOP_SPANS times the longest loop (Stealthy Steps' 5173
against 5120) is left out: the song ends where it did and the other loops drift each time round
(PreparedSong.loops_drift).

The song given is left as it is.
"""

import copy
import math
from dataclasses import dataclass

from .effects import ChanTempoDiv, SetTempoDiv
from .song import SmpsSong

_MAX_LOOP_SPANS = 4         # a common loop period this many times the longest loop, at most


@dataclass(frozen=True)
class PreparedSong:
    song: SmpsSong
    tempo_div_changes: tuple[tuple[int, int], ...]   # (tick, divider) of each smpsSetTempoDiv
    loops_extended: tuple[dict, ...]                 # {label, before, after, span} per channel extended
    loops_drift: tuple[str, ...] = ()                # tracks whose loops the MOD's cannot keep in step


def prepare_song(song: SmpsSong) -> PreparedSong:
    """`song` as the driver plays it, a new song: re-timed for smpsSetTempoDiv, short loop
    bodies replayed to its end."""
    prepared = copy.deepcopy(song)
    changes = _apply_global_tempo_div(prepared)
    end, drift = _loop_end(prepared)
    extended = _extend_looping_channels(prepared, end)
    return PreparedSong(prepared, tuple(changes), tuple(extended), drift)


def _loop_end(song: SmpsSong) -> tuple[int, tuple[str, ...]]:
    """Where every track is replayed to: the last loop start plus the loops' common period, at
    least the song's end; and the tracks out of step when that period is too long to unroll."""
    end, start = song.end_tick(), song.loop_target_tick() or 0
    spans = {ch.header.chip_channel or ch.header.label: span for ch in song.channels
             if (span := _loop_period(ch)) is not None}
    longest = max(spans.values(), default=0)
    spans = {name: span for name, span in spans.items() if span * _MAX_LOOP_SPANS >= longest}
    if len(set(spans.values())) < 2:
        return end, ()
    period = math.lcm(*spans.values())
    if period > _MAX_LOOP_SPANS * longest:
        return end, tuple(name for name, span in spans.items() if (end - start) % span)
    return max(end, start + period), ()


def _loop_period(ch) -> int | None:
    """The ticks a jumping track's loop repeats after: the shortest divisor of its body's length
    (its first pass's end less the jump target) that the body's events repeat at."""
    if not ch.has_jump or ch.loop_tick is None or not ch.events:
        return None
    ch_end = max(ev.tick_position + (ev.note.duration if ev.note else 0) for ev in ch.events)
    span = ch_end - ch.loop_tick
    if span <= 0:
        return None
    body = [(ev.tick_position - ch.loop_tick, _signature(ev)) for ev in ch.events if ev.tick_position >= ch.loop_tick]
    for period in range(1, span // 2 + 1):
        if span % period == 0 and _repeats(body, period, span):
            return period
    return span


def _repeats(body: list[tuple[int, tuple]], period: int, span: int) -> bool:
    """The body's events from `period` on are those before `span - period`, `period` later."""
    head = [(t, sig) for t, sig in body if t < span - period]
    tail = [(t - period, sig) for t, sig in body if t >= period]
    return head == tail


def _signature(ev) -> tuple:
    """What an event plays, its tick aside."""
    n = ev.note
    if n is None:
        return (repr(ev.effect),)
    return (n.note_value, n.duration, n.is_rest, n.is_no_attack, n.dac_name)


def _apply_global_tempo_div(song: SmpsSong) -> list[tuple[int, int]]:
    """Re-time every channel for smpsSetTempoDiv (cfSetTempoDividerAll), which writes a new
    TempoDivider into EVERY track.  Returns the [(tick, divider)] changes found (Credits: $02
    then $01 in the DAC track, a half-tempo passage).

    The parser scaled each channel's durations by its OWN divider only (the header value and
    smpsChanTempoDiv).  The driver multiplies a duration by the track's divider when the note
    is READ, so a note begun before the change keeps its length and the first note read after
    it takes the new one; the last write wins, whether it was the track's own smpsChanTempoDiv
    or the global flag.  The carrying channel is re-timed first (the change's real tick
    depends on any earlier change), then the others.  Labels (loop targets) are not re-timed.
    """
    found = [ch for ch in song.channels
             if any(isinstance(ev.effect, SetTempoDiv) for ev in ch.events)]
    if not found:
        return []
    header_div = song.header.tempo_divider
    changes: list[tuple[int, int]] = []

    def retime(ch, changes):
        own_parse = header_div          # divider the parser used for the next note
        own_div, own_tick = header_div, -1
        act = 0
        new_changes = []
        for ev in ch.events:
            ev.tick_position = act
            if ev.is_note:
                d_raw = ev.note.duration / own_parse
                g = [c for c in changes if c[0] <= act and c[0] > own_tick]
                div = g[-1][1] if g else own_div
                ev.note.duration = round(d_raw * div)
                act += ev.note.duration
                continue
            match ev.effect:
                case ChanTempoDiv(divider=divider):
                    own_parse = own_div = divider
                    own_tick = act
                case SetTempoDiv(divider=divider):
                    # Writes this track's divider too (cfSetTempoDividerAll covers every track).
                    own_div, own_tick = divider, act
                    new_changes.append((act, divider))
        return new_changes

    for ch in found:
        changes = sorted(set(changes) | set(retime(ch, changes)))
    for ch in song.channels:
        if ch not in found:
            retime(ch, changes)
    return changes


def _extend_looping_channels(song: SmpsSong, global_last_tick: int) -> list[dict]:
    """Extend channels whose event data ends early due to a compact smpsJump inner loop; one
    {label, before, after, span} per channel extended (events before and after, the body's ticks).

    If a channel has has_jump=True and its last event tick is less than `global_last_tick`
    (_loop_end), repeat the loop body (events from jump_target_tick onward) until channel
    coverage reaches it.

    Example: PSG3 in GHZ — loop body = {NOTE nMaxPSG dur=8 at tick=48}, loop_span=8.
    Without extension: 4 events / 56 ticks. After: ~1200 events / full song.
    """
    infos: list[dict] = []

    for ch in song.channels:
        loop_start_tick = ch.loop_tick
        if not ch.has_jump or loop_start_tick is None or not ch.events:
            continue
        ch_last = max(ev.tick_position + (ev.note.duration if ev.note else 0) for ev in ch.events)
        if ch_last >= global_last_tick:
            continue  # Already covers full song; skip


        # Loop body = the events after the jump label.  Selecting by tick alone would also
        # replay a coordination flag written just BEFORE the label at the same tick on every
        # repetition (SYZ PSG3: `smpsPSGAlterVol $FF` / `Jump03:` — the hi-hat crept from
        # attenuation 5 to 0 in five loops; on hardware it stays at 5).
        body_index = ch.loop_event_index
        if body_index is not None:
            loop_body = ch.events[body_index:]
        else:
            loop_body = [ev for ev in ch.events if ev.tick_position >= loop_start_tick]
        if not loop_body:
            continue

        # Loop span = (last body event end tick) − loop_start_tick
        last_ev = loop_body[-1]
        loop_end = last_ev.tick_position + (last_ev.note.duration if last_ev.note else 0)
        loop_span = loop_end - loop_start_tick
        if loop_span <= 0:
            continue

        # Synthesize additional iterations until we reach global_last_tick; each one's first
        # note tied as the jump back leaves it (replay_tie)
        first_note = next((ev for ev in loop_body if ev.note is not None), None)
        original_count = len(ch.events)
        offset = ch_last - loop_start_tick
        while (loop_start_tick + offset) < global_last_tick:
            for ev in loop_body:
                new_tick = ev.tick_position + offset
                if new_tick >= global_last_tick:
                    break
                new_ev = copy.copy(ev)
                new_ev.note = copy.copy(ev.note) if ev.note else None
                new_ev.tick_position = new_tick
                if new_ev.note:
                    cap_dur = global_last_tick - new_tick
                    new_ev.note.duration = min(new_ev.note.duration, cap_dur)
                    if ev is first_note and ch.replay_tie is not None:
                        new_ev.note.is_no_attack = ch.replay_tie
                ch.events.append(new_ev)
            offset += loop_span

        infos.append({
            'label': ch.header.label,
            'before': original_count,
            'after': len(ch.events),
            'span': loop_span,
        })
    return infos
