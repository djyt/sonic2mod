"""The parsed song re-timed and unrolled the way the driver plays it, before anything counts notes.

    parsed song ──► apply_global_tempo_div (smpsSetTempoDiv re-times every track)
                ──► extend_looping_channels (a short jump loop replayed to the song's end)
"""

import copy

from .smps_song import SmpsSong


def apply_global_tempo_div(song: SmpsSong) -> list[tuple[int, int]]:
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
             if any(ev.is_effect and ev.effect.effect_type == 'smpsSetTempoDiv' for ev in ch.events)]
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
            kind = ev.effect.effect_type
            if kind == 'smpsChanTempoDiv':
                own_parse = own_div = ev.effect.params[0]
                own_tick = act
            elif kind == 'smpsSetTempoDiv':
                # Writes this track's divider too (cfSetTempoDividerAll covers every track).
                own_div, own_tick = ev.effect.params[0], act
                new_changes.append((act, ev.effect.params[0]))
        return new_changes

    for ch in found:
        changes = sorted(set(changes) | set(retime(ch, changes)))
    for ch in song.channels:
        if ch not in found:
            retime(ch, changes)
    return changes


def extend_looping_channels(song: SmpsSong) -> list[dict]:
    """Extend channels whose event data ends early due to a compact smpsJump inner loop; one
    {label, before, after, span} per channel extended (events before and after, the body's ticks).

    If a channel has has_jump=True and its last event tick is less than the global
    last tick across all channels, repeat the loop body (events from
    jump_target_tick onward) until channel coverage reaches global_last_tick.

    Example: PSG3 in GHZ — loop body = {NOTE nMaxPSG dur=8 at tick=48}, loop_span=8.
    Without extension: 4 events / 56 ticks. After: ~1200 events / full song.
    """
    label_tick_pos = song.label_tick_pos
    global_last_tick = song.end_tick()
    infos: list[dict] = []

    for ch in song.channels:
        if not ch.has_jump or not ch.jump_target_label or not ch.events:
            continue
        ch_last = max(ev.tick_position + (ev.note.duration if ev.note else 0) for ev in ch.events)
        if ch_last >= global_last_tick:
            continue  # Already covers full song; skip

        loop_start_tick = label_tick_pos.get(ch.jump_target_label)
        if loop_start_tick is None:
            continue

        # Loop body = the events after the jump label.  Selecting by tick alone would also
        # replay a coordination flag written just BEFORE the label at the same tick on every
        # repetition (SYZ PSG3: `smpsPSGAlterVol $FF` / `Jump03:` — the hi-hat crept from
        # attenuation 5 to 0 in five loops; on hardware it stays at 5).
        body_index = ch.label_event_index.get(ch.jump_target_label)
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

        # Synthesize additional iterations until we reach global_last_tick
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
                ch.events.append(new_ev)
            offset += loop_span

        infos.append({
            'label': ch.header.label,
            'before': original_count,
            'after': len(ch.events),
            'span': loop_span,
        })
    return infos
