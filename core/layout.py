"""The MOD laid out once every channel is converted: what goes into the cells the notes left free.

    leading rests' C00 (pattern 0 row 0)  ->  BPM / speed Fxx  ->  mid-song tempo Fxx
    (after mod_pattern_breaks)  ->  the loop's Bxx (+ Dxx, + the target segment's BPM)

Each command takes a free effect slot; one that finds none is reported, not forced.
"""

from .config import ConversionConfig
from .diagnostics import Diagnostics, InfoKind, WarningKind
from .mod import ModFile, row_to_bcd, shift_for_breaks
from .smps_song import SmpsSong
from .timeline import Timeline

# ProTracker's default speed: no Fxx needed for it
_DEFAULT_SPEED = 6


class ModLayout:
    def __init__(self, mod: ModFile, config: ConversionConfig, song: SmpsSong, timeline: Timeline,
                 diag: Diagnostics) -> None:
        self._mod = mod
        self._config = config
        self._song = song
        self._timeline = timeline
        self._diag = diag

    def leading_rests(self, rests: dict[int, str]) -> None:
        """C00 at pattern 0 row 0 for every channel whose first event is a rest.

        Nothing plays there on the first pass, but a song that loops to position 0 (Robotnik,
        Special Stage) comes back with the last note before the Bxx still ringing, and this
        C00 is what ends it: without one the note rang through the leading rest until the
        channel's next event.  The Fxx speed and BPM commands are placed after this
        (tempo_commands) in the cells left free.  A note delayed into row 0 (EDx)
        restarts the sample itself.  When no cell is free the C00 is dropped, with a warning
        if the loop does return to row 0 (Star Light rests on all nine channels but loops to
        position 1: no warning).
        """
        if not rests:
            return
        loops_to_row0 = self._song.loop_target_tick() == 0
        used = {c.mod_channel for c in self._config.channels if c.enabled}
        order = ([c for c in range(self._mod.CHANNELS) if c not in used]
                 + [c for c in sorted(used) if c not in rests])
        for ch in sorted(rests):
            if self._mod.note_at(0, 0, ch):
                continue
            eff, par = self._mod.effect_at(0, 0, ch)
            if (eff, par) != (0, 0):
                slot = self._mod.free_effect_channel(0, 0, order)
                if slot is None:
                    if loops_to_row0:
                        self._diag.warn(WarningKind.REST_NO_SLOT, channel=rests[ch], mod_channel=ch)
                    continue
                self._mod.set_cursor(0, slot, 0)
                self._mod.set_effect(eff, par)
            self._mod.set_cursor(0, ch, 0)
            self._mod.set_effect(0xC, 0)

    def tempo_commands(self, rests: dict[int, str]) -> None:
        """The BPM and speed (Fxx) on pattern 0 row 0, in cells whose effect slot is free.

        They used to be written before the channels, on channels 0 and 1, where a note's own
        effect on row 0 (a Cxx, a legato 3FF) silently overwrote them: Special Stage lost its
        speed 3 and played at half tempo.  Now they go last: a spare channel first, then any
        channel whose row-0 cell has no effect; failing that, a leading rest's C00 gives way
        (the tempo matters more than one ring through the first rest).
        """
        wanted = [(0xF, self._config.target_bpm)]
        if self._config.target_speed != _DEFAULT_SPEED:
            wanted.insert(0, (0xF, self._config.target_speed))
        used = {c.mod_channel for c in self._config.channels if c.enabled}
        order = [c for c in range(self._mod.CHANNELS) if c not in used] + sorted(used)
        for eff, par in wanted:
            slot = self._mod.free_effect_channel(0, 0, order)
            if slot is None:
                # Take a leading rest's C00 (a cell with no note); a sample restart (EDx)
                # or a note's own command stays.  Worth a warning only where the song loops
                # to row 0, as in leading_rests: a song that ends (smpsStop, no jump)
                # has keyed every track off before the player wraps (the Title Screen).
                for ch in order:
                    if not self._mod.note_at(0, 0, ch) and self._mod.effect_at(0, 0, ch) == (0xC, 0):
                        slot = ch
                        if self._song.loop_target_tick() == 0:
                            self._diag.warn(WarningKind.REST_NO_SLOT, mod_channel=ch,
                                            channel=rests.get(ch, f'MOD channel {ch}'))
                        break
            if slot is None:
                self._diag.warn(WarningKind.TEMPO_NO_SLOT, pattern=0, row=0,
                                modifier=self._song.header.tempo_modifier, bpm=par)
                continue
            self._mod.set_cursor(0, slot, 0)
            self._mod.set_effect(eff, par)

    def tempo_changes(self) -> None:
        """Fxx (set BPM) on the row of every smpsSetTempoMod, in a cell whose effect slot is free.

        Spare MOD channels are tried first, then any channel's cell without an effect, then a
        cell holding only a 4xy continuation (vibrato is the least of the three).  Drowning
        speeds up in four steps this way; the header tempo is still the song's own BPM.
        """
        used = {c.mod_channel for c in self._config.channels if c.enabled}
        order = [c for c in range(self._mod.CHANNELS) if c not in used] + sorted(used)
        for start, modifier in self._timeline.segments[1:]:
            bpm = self._timeline.bpm_for(modifier)
            exact = self._config.target_bpm * self._timeline.ticks_per_frame(modifier) / self._timeline.ticks_per_frame(self._song.header.tempo_modifier)
            pattern, row = self._timeline.pattern_row(start)
            if pattern >= self._config.max_patterns:
                break
            self._mod.ensure_pattern(pattern)
            slot = None
            for want_free in (True, False):
                for ch in order:
                    eff, par = self._mod.effect_at(pattern, row, ch)
                    free = eff == 0 and par == 0
                    vib_only = eff == 0x4 and not self._mod.note_at(pattern, row, ch)
                    if free if want_free else vib_only:
                        slot = ch
                        break
                if slot is not None:
                    break
            change = {'tick': start, 'pattern': pattern, 'row': row, 'modifier': modifier, 'bpm': bpm,
                      'exact_bpm': exact}
            if slot is None:
                self._diag.warn(WarningKind.TEMPO_NO_SLOT, channel='all', **change)
                continue
            self._mod.set_cursor(pattern, slot, row)
            self._mod.set_effect(0xF, bpm)
            self._diag.info(InfoKind.TEMPO_CHANGE, **change)
            if not 32 <= exact <= 255:
                self._diag.warn(WarningKind.TEMPO_BPM_RANGE, channel='all', **change)

    def loop_point(self, breaks=None) -> None:
        """Set Bxx position jump for song looping based on smpsJump targets.

        Must be called after apply_pattern_breaks so that the Bxx is placed
        at the correct post-break location and the target maps correctly.

        breaks: list of (pattern_slot, break_row) tuples from mod_pattern_breaks.
                When provided, the loop target tick is mapped to its post-break
                position by applying each break's shift in sorted order.
        """
        loop_target_tick = self._song.loop_target_tick()
        if loop_target_tick is None:
            return  # No smpsJump found; nothing to do

        tpr = self._timeline.ticks_per_row

        # Derive last row from song tick data (handles rest/sustain tails that
        # a period-scan could not see because they have no note trigger).
        song_end_tick = self._song.end_tick()
        song_end_flat = shift_for_breaks(max(round(song_end_tick / tpr), 1) - 1, breaks)
        last_pattern, last_row = divmod(song_end_flat, 64)

        # Target: map loop_target_tick to post-break (pattern, row)
        flat_row = shift_for_breaks(round(loop_target_tick / tpr), breaks)
        target_pattern, target_row = divmod(flat_row, 64)

        # The Bxx takes a free effect slot on the last row (channel 0 held a note cut there -
        # Green Hill's EC1 - and the jump overwrote it), among the columns already in use so a
        # merged build is not widened for it.  A Dxx must sit to its right: ProTracker reads a
        # row's effects left to right and a Bxx after a Dxx resets the break row to 0.
        cols = range(max(1, self._mod.used_channels()))
        need = 2 if target_row != 0 else 1
        free = [c for c in cols if self._mod.effect_slot_free(last_pattern, last_row, c)]
        b_chan = free[0] if len(free) >= need else 0
        if len(free) < need:
            eff = self._mod.effect_at(last_pattern, last_row, 0)
            self._diag.warn(WarningKind.LOOP_NO_SLOT, channel='all', pattern=last_pattern, row=last_row,
                            overwrote=eff)
        self._mod.set_cursor(last_pattern, b_chan, last_row)
        self._mod.set_position_jump(target_pattern)

        # A song that loops back into a different tempo segment needs its BPM set again there
        # (the Fxx cells written by tempo_changes sit at the changes, not at the target).
        segs = self._timeline.segments
        if len(segs) > 1 and self._timeline.segment_at(loop_target_tick)[1] != segs[-1][1]:
            target_mod = self._timeline.segment_at(loop_target_tick)[1]
            ch = self._mod.free_effect_channel(target_pattern, target_row)
            if ch is not None:
                self._mod.set_cursor(target_pattern, ch, target_row)
                self._mod.set_effect(0xF, self._timeline.bpm_for(target_mod))
            else:
                self._diag.warn(WarningKind.TEMPO_NO_SLOT, channel='all', tick=loop_target_tick,
                                pattern=target_pattern, row=target_row, modifier=target_mod,
                                bpm=self._timeline.bpm_for(target_mod), exact_bpm=float('nan'))

        # If the target lands mid-pattern, write a Dxx companion on a free channel
        if target_row != 0:
            ch = self._mod.free_effect_channel(last_pattern, last_row, range(b_chan + 1, self._mod.CHANNELS))
            if ch is not None:
                self._mod.set_channel(ch)
                self._mod.set_effect(0xD, row_to_bcd(target_row))

        self._diag.info(InfoKind.LOOP_SET, pattern=last_pattern, row=last_row, target=target_pattern)
