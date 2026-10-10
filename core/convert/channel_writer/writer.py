"""ChannelWriter: one SMPS channel into the MOD's cells, event by event (the package's docstring
has the map)."""

import bisect
import math
from dataclasses import dataclass

from ...config import ChannelConfig
from ...diagnostics import WarningKind
from ...merge import Composite
from ...mod import MOD_MAX_VOLUME, MOD_NOTE_MAP, PERIOD_TABLE, ModNote, note_rate
from ...plan import DriverState, ResolvedNote, detune_cents, walk_channel
from ...smps import AlterVol, ChannelType, NoteFill, PanStep, SetVol, SmpsChannel
from .cells import Cells
from .context import WriterContext
from .fades import Fades
from .levels import Levels
from .modulation import Modulation
from .note import FillCut, NoteOn
from .note_warnings import warn_resolution
from .router import ColumnRouter

# A pan's 8xx: FT2's panning, 00 left .. 80 centre .. FF right
_PANNING = {"L": 0x00, "C": 0x80, "R": 0xFF}


@dataclass
class _Ring:
    """What the channel's last note-on left sounding."""
    cell: tuple[int, int] | None = None     # (pattern, row) it went to
    inst: int | None = None                 # its instrument; None once nothing rings
    volume: int = MOD_MAX_VOLUME            # the MOD volume it plays at (a sliding loop's: fallen to)
    index: int = 0                          # its MOD note
    chip: int | None = None                 # the chip pitch it sounds
    voice: int | None = None                # the FM voice it was played with
    # The detune a tie (smpsNoAttack + a duration) sounds at: the driver writes the frequency
    # with the track's Detune on a tie too, so Sonic 1's Scrap Brain Zone FM4 scoop
    # (`smpsAlterNote $EC`, `nG5, $02`, `smpsAlterNote $00`, `smpsNoAttack, $06`) rises 49 c on
    # its tie.  The MOD note keeps its detune variant's sample there: an E1x / E2x moves its
    # period instead.
    cents: float | None = None              # cents it plays off its table pitch
    period: int = 0                         # its MOD period, slides included
    gain_db: float = 0.0                    # dB a unison chord adds to it (ResolvedNote.gain_db)
    member: Composite | None = None         # the banked composite it plays (core.merge.banks): measured
                                            # under its own id, released at its primary's rate


class ChannelWriter:
    """One channel's events into the MOD's cells."""

    def __init__(self, ctx: WriterContext, channel: SmpsChannel, chan_cfg: ChannelConfig):
        self._ctx = ctx
        self._config = ctx.config
        self._timeline = ctx.timeline
        self._channel = channel
        self._cfg = chan_cfg
        kind = channel.header.channel_type
        self._is_dac = kind == ChannelType.DAC     # by the song: Type 0 FM's drums are FM3
        self._is_psg = kind == ChannelType.PSG
        self._track = channel.rules.track(kind)    # how its driver plays this kind
        self._rest_plays_out = self._is_dac and not self._track.rest_cuts
        self._col = chan_cfg.mod_channel           # the column the last note-on or rest wrote to
        self._cells = Cells(ctx)
        self._router = ColumnRouter(ctx, self._cells, chan_cfg.source, chan_cfg.mod_channel)

        # Driver state: level, pan, transpose, FM voice and the active PSG entry — including
        # the smpsHeaderPSG voice.  Shared with the level pre-passes and the rate-3 derivation
        # so the four passes cannot disagree about what a note plays.
        self._st = DriverState.for_channel(channel, ctx.config, chan_cfg.instrument)
        if self._is_dac or (not self._is_psg and ctx.fm_volume_mode == "off"):
            self._st.tl = 0          # neither mode reads the smpsHeaderFM volume byte

        # MOD-emission state, which the driver knows nothing about
        self._note_fill = 0
        self._pan_steps: list[tuple[int, str]] = []       # (tick, side) of each pan animation step
        self._range_entry = None   # voice_map InstrumentRange matched on most recent note
        self._levels = Levels(ctx, channel, chan_cfg, self._st)
        self._modulation = Modulation(ctx, self._cells, channel, chan_cfg.source, self._track)
        self._fades = Fades(ctx, self._cells)
        self._ring = _Ring()
        self._dac_map = {dac_cfg.name: dac_cfg for dac_cfg in ctx.config.dac_samples}

        self._ring_ticks = self._collect_ring_ticks()
        # PSG auto note-cut: hardware PSGDoNext sets vol=15 when note duration expires.  The
        # note-on (pattern, row) positions, so no C00 is placed where a later set_note writes a
        # note (set_note retains effect bytes, so a pre-placed C00 would silence the trigger).
        self._note_on_positions = self._note_on_cells() if self._is_psg else set()
        # Every note-on tick of this channel (spliced notes included): a release slide runs up to
        # the row before the next one, so it never lands in a note-on's cell
        self._note_on_ticks = sorted(ev.tick_position for ev in self._note_ons())

    def _note_ons(self):
        """The note-on events this channel's output sounds (spliced ones included)."""
        return (ev for ev in self._channel.events
                if ev.note is not None and not ev.note.is_rest and self._router.plays_here(ev))

    def _note_on_cells(self) -> set[tuple[int, int]]:
        """(pattern, row) of every note-on: the row it rounds to and the row it starts in."""
        tpr = self._timeline.ticks_per_row
        found: set[tuple[int, int]] = set()
        for ev in self._note_ons():
            found.add(self._timeline.pattern_row(ev.tick_position))
            found.add(Cells.cell(int(ev.tick_position // tpr)))
        return found

    def _collect_ring_ticks(self) -> dict[int, int]:
        """How long each note-on rings before a PSG duration cut: its own duration plus the
        smpsNoAttack continuations after it (`nE5, $34, smpsNoAttack, $34` holds 104 ticks on
        the hardware; Sonic 1's Green Hill Zone's last verse chord cut its chime at 52)."""
        ring_ticks: dict[int, int] = {}
        ringing = None
        for ev in self._channel.events:
            note = ev.note
            if note is None:
                continue
            if note.is_rest and note.is_no_attack and ringing is not None:
                ring_ticks[id(ringing)] += note.duration
                continue
            ringing = None if note.is_rest else ev
            if ringing is not None:
                ring_ticks[id(ev)] = note.duration
        return ring_ticks

    # --- the walk -------------------------------------------------------------------------------
    def write(self) -> None:
        for event, st, res in walk_channel(self._channel, self._config, self._cfg, self._st):
            # A follower's solo note on a merged channel arrives with the follower's state
            # (core.merge); every other event with this channel's own.
            self._st = st
            if event.is_effect:
                self._on_effect(event)
                continue
            if not event.is_note:
                continue

            note = event.note
            if not note.is_rest and not self._router.plays_here(event):
                self._on_folded(event.tick_position)
                continue
            if note.is_rest:
                self._on_rest(event)
                continue
            if not self._on_note(event, res):
                self._write_pan_steps()
                return
        self._on_stop()
        self._write_pan_steps()

    def _on_stop(self) -> None:
        """smpsStop keys the track off (StopTrack → FMNoteOff): an FM note still ringing at the
        track's end releases there, as a rest would end it.  Sonic 1's Stage Clear closing chord
        held its looped samples through the end and into the song's restart.  A channel that loops
        never stops; the DAC plays its sample out, and a PSG note is cut at its duration."""
        if self._channel.has_jump or self._is_dac or self._is_psg or self._ring.inst is None:
            return
        notes = [(ev, ev.note) for ev in self._channel.events if ev.note is not None]
        if not notes:
            return
        last, note = notes[-1]
        if note.is_rest and not note.is_no_attack:
            return                      # keyed off already
        if not note.is_rest and not self._router.plays_here(last):
            return                      # folded onto another channel: it ends there

        end = last.tick_position + note.duration
        pattern, row = self._timeline.pattern_row(end)
        if not self._cells.in_song(pattern):
            return
        self._col = self._router.current(end)
        self._cells.ensure(pattern)     # an end on a pattern's first row: none written yet
        if not self._cells.note_at(pattern, row, self._col):
            self._key_off(pattern, row, end)

    def _on_effect(self, event) -> None:
        """walk_channel has advanced st past this flag: level, pan, transpose, FM voice and PSG
        instrument routing.  What is left is MOD-emission state.  smpsSetvoice, smpsPan,
        smpsChangeTransposition, smpsPSGform and smpsPSGvoice are st.apply's business; smpsNop
        has no MOD equivalent; smpsAlterNote is a raw FNUM offset (~10 cents) that does not
        affect note pitch or voice_map lookup."""
        eff = event.effect
        if isinstance(eff, PanStep):
            self._pan_steps.append((event.tick_position, eff.side))
        elif isinstance(eff, (AlterVol, SetVol)):
            self._levels.on_change(eff, self._st)
        elif isinstance(eff, NoteFill):
            self._note_fill = eff.frames
        else:
            self._modulation.apply(eff, event.tick_position)

    def _write_pan_steps(self) -> None:
        """Each pan animation step as 8xx on its row, where no other effect is (D2: the lowest of
        all); a step to the pan last written writes nothing."""
        written = None
        for tick, side in self._pan_steps:
            pattern, row = self._timeline.pattern_row(tick)
            if side == written or not self._cells.in_song(pattern):
                continue
            col = self._router.current(tick)
            if self._cells.free(pattern, row, col):
                self._cells.put(pattern, row, col, 0x8, _PANNING[side])
                written = side

    def _on_folded(self, tick: int) -> None:
        """This note plays on its group's primary channel in this pattern (merge_patterns), or
        nowhere (dropped).  What still rings here from a pattern the channel was live in ends
        now, as the re-key ended it on the hardware; the channel's cells stay empty otherwise."""
        if self._ring.inst is None:
            return
        pattern, row = self._timeline.pattern_row(tick)
        if not self._cells.in_song(pattern):
            return
        self._col = self._router.current(tick)
        if not self._cells.note_at(pattern, row, self._col):      # else a note-on already takes the column
            self._key_off(pattern, row, tick)
        self._ring.inst = None

    def _on_rest(self, event) -> None:
        note, tick = event.note, event.tick_position

        # The drum track's rest plays nothing new unless its driver's cuts: the sample plays out
        # (Sonic 1's DACUpdateTrack returns on $80), Type 0 FM lets an FM drum ring on FM3, Streets
        # of Rage's plays the empty sample.  A follower's rest spliced in still cuts.
        if self._rest_plays_out and getattr(event, "merged", None) is None:
            return

        # is_no_attack=True marks an FM/DAC standalone-duration continuation — the YM2612
        # envelope sustains naturally; do not emit C00.
        if note.is_no_attack:
            if self._ring.cents is not None and self._ring.inst is not None and not self._st.is_psg:
                self._retune_tie(tick)
            return
        self._stop_ringing(event)

    def _stop_ringing(self, event) -> None:
        """End what rings on the channel's column at `event`: a rest, or a silent drum's hit."""
        tick = event.tick_position
        pattern, row = self._timeline.pattern_row(tick)
        if not self._cells.in_song(pattern):
            return
        self._cells.ensure(pattern)
        if self._ring.inst is None and self._router.away(event):
            return                  # nothing of this channel's sounds here: no C00 clutter
        self._col = self._router.current(tick)
        if (pattern, row) == (0, 0):
            # A leading rest.  Its C00 matters once the song loops back to position 0, and the
            # cell may hold a tempo command, so it is placed after every channel is converted
            # (ModLayout.leading_rests).
            self._ctx.leading_rests[self._col] = self._cfg.source
            return

        # Another channel's notes take this column here (mod_channel): its note-on ends this ring
        if self._router.borrowed(self._col, tick) and self._cells.note_at(pattern, row, self._col):
            return
        self._key_off(pattern, row, tick)

    def _key_off(self, pattern: int, row: int, tick: int) -> None:
        """End what rings on the column: the note fades at the voice's release rate (the sample
        loops, or would be cut short of the chip's release either way), else C00.  No slide on
        a column another channel's notes take (mod_channel): it would sit on their notes."""
        rate = None if self._router.borrowed(self._col, tick) else self._release_rate(self._ring.inst, tick)
        if rate is None:
            self._cells.put(pattern, row, self._col, 0xC, 0)
            return
        self._fades.release(self._col, Cells.row_total(pattern, row), self._ring.volume, rate, tick,
                            self._next_note_row(tick))

    def _on_note(self, event, res: ResolvedNote | None) -> bool:
        """A note-on; False once the song runs past max_patterns (the channel stops)."""
        pattern, _ = self._timeline.pattern_row(event.tick_position)
        if not self._cells.in_song(pattern):
            self._ctx.diag.warn(WarningKind.PATTERN_OVERFLOW, channel=self._cfg.source, pattern=pattern,
                                max=self._config.max_patterns)
            return False
        self._cells.ensure(pattern)     # its cells are read before a note-on writes there

        if self._is_dac and res is None:
            return self._on_dac(event)
        assert res is not None
        return self._on_melodic(event, res)

    def _open_cell(self, pattern: int, row: int, tick: int) -> None:
        """Take the column a note-on at `tick` goes to, a stale C00 in its cell cleared."""
        self._col = self._router.take(pattern, row, tick, self._ring.inst is not None)
        self._cells.clear_stale_cut(pattern, row, self._col)
        self._ring.cell = (pattern, row)

    # --- DAC ------------------------------------------------------------------------------------
    def _on_dac(self, event) -> bool:
        """A drum hit: its dac_samples instrument and note.  DAC notes carry no other effect, so
        the slot is always free for EDx."""
        note, tick = event.note, event.tick_position

        # A silent FM drum (its program a rest): the hit only stops the drum ringing, as FM3's key-off
        drum = self._ctx.song.fm_drums.get(note.dac_name)
        if drum is not None and drum.silent:
            self._stop_ringing(event)
            return True

        pattern, row, delay = self._cells.note_cell(tick, True, None, self._ring.cell)
        if not self._cells.in_song(pattern):
            return False
        self._open_cell(pattern, row, tick)

        dac_cfg = self._dac_map.get(note.dac_name)
        if dac_cfg:
            delay = self._play_drum(pattern, row, tick, dac_cfg, delay)
        else:
            # Fallback: use default instrument and C3
            self._cells.put_note(pattern, row, self._col, ModNote.C3, self._st.instrument)
            self._ring.inst, self._ring.volume = self._st.instrument, MOD_MAX_VOLUME
        if delay:
            self._cells.put(pattern, row, self._col, 0xE, 0xD0 | delay)
        return True

    def _play_drum(self, pattern: int, row: int, tick: int, dac_cfg, delay: int) -> int:
        """The drum's instrument and note (a composite with its hi-hat folded in) on its cell.
        Returns the EDx delay left: a sound inside a sample bank starts with 9xx instead."""
        plan = self._ctx.merge
        dac_inst, dac_note, region = self._router.drum(
            tick, dac_cfg.mod_instrument, MOD_NOTE_MAP[dac_cfg.mod_note])
        self._cells.put_note(pattern, row, self._col, dac_note, dac_inst)
        self._played(dac_inst, tick)
        self._ring.inst, self._ring.volume = dac_inst, self._levels.sample(dac_inst)
        self._ring.member = plan.bank_members.get((self._cfg.source, tick)) if plan is not None else None
        if region is None:
            return delay

        # A sound inside a sample bank (core.merge.banks): start at its offset and cut the note
        # once it is over, before the next sound in the slot
        offset, sound = region
        if offset:
            self._cells.put(pattern, row, self._col, 0x9, offset >> 8)
            if delay:
                delay = 0            # the slot holds the offset
                self._ctx.stats.bank_delays_dropped += 1
        self._cut_bank_sound(tick, dac_note.value, sound)
        return delay

    # --- melodic --------------------------------------------------------------------------------
    def _on_melodic(self, event, res: ResolvedNote) -> bool:
        """Which instrument and which MOD note (resolve_note, through walk_channel: the range
        lookup in the config's range_space, root + (key - low) for an anchored entry, the channel
        transpose otherwise), then the note-on and the commands on its rows."""
        n = self._melodic_note(event, res)
        self._resolve_legato(n)
        # After legato: a strict legato plays the sounding instrument, whose level is what counts
        n.volume = self._emit_volume(n.instrument)
        n.needs_cxx = n.volume != self._levels.sample(n.instrument)

        # Where the note goes (see Cells.note_cell): on its own row with an EDx delay when it
        # starts between rows and the effect slot is free
        slot_free = not n.legato and not n.bank9 and (not n.needs_cxx
                                                      or n.duration >= 2 * self._timeline.ticks_per_row)
        n.pattern, n.row, n.delay = self._cells.note_cell(n.tick, slot_free, n.cut_tick, self._ring.cell)
        if not self._cells.in_song(n.pattern):
            return False
        self._open_cell(n.pattern, n.row, n.tick)
        self._cells.put_note(n.pattern, n.row, self._col, n.mod_note, n.instrument)
        self._played(n.instrument, n.tick)
        self._ring_note(n)

        # Its cut, then the attack row's commands, the level a displaced Cxx sets on a later row
        fill = self._place_fill(n)
        if n.psg and not fill.placed:
            self._place_duration_cut(n)
        cxx_cell, slot_used = self._attack_commands(n, fill.slot_used)

        # The attack row's free slot: Cxx when the note's level differs from its instrument's
        # (MOD resets to the sample volume on each note trigger, so none is needed otherwise),
        # else the modulation's 4xy
        if not slot_used and n.needs_cxx:
            self._cells.put(n.pattern, n.row, self._col, 0xC, n.volume)
        self._modulation.write(n, self._st, self._range_entry, self._col, not slot_used and not n.needs_cxx,
                               fill, cxx_cell)

        # A sliding loop's fall, and a banked sound's end
        self._write_decay(n, fill)
        self._cut_banked(n)
        return True

    def _ring_note(self, n: NoteOn) -> None:
        """The note-on is what rings now."""
        ring = self._ring
        ring.inst, ring.volume = n.instrument, n.volume
        ring.index, ring.chip, ring.voice = n.mod_note.value, n.res.chip, self._st.voice
        if self._ctx.detune is not None and not n.psg:
            ring.cents = self._ctx.sample_cents(n.instrument)
            ring.period = PERIOD_TABLE[n.mod_note.value]

    def _played(self, inst: int, tick: int) -> None:
        """Note which source plays `inst`: this channel, or the follower a solo note came from."""
        plan = self._ctx.merge
        solo = plan.solo.get((self._cfg.source, tick)) if plan is not None else None
        self._ctx.played.setdefault(inst, set()).add(solo[0] if solo else self._cfg.source)

    def _melodic_note(self, event, res: ResolvedNote) -> NoteOn:
        """The note as resolved, with what its placement depends on: its fill or duration cut,
        its sound inside a sample bank."""
        note, tick = event.note, event.tick_position
        plan = self._ctx.merge
        self._ring.gain_db = res.gain_db
        self._range_entry = None if self._is_psg else res.entry
        warn_resolution(self._ctx.diag, self._config, self._cfg.source, self._st, res, note)

        # A solo note carries none of this channel's modulation, but its own note fill (the
        # follower's smpsNoteFill, on the NoteOn core.merge spliced it from), and a PSG note
        # ends at its duration wherever it plays (an FM one on a PSG channel does not: it rings
        # into its rest's release)
        solo = getattr(event, "merged", None)
        fill = self._note_fill if solo is None else solo.fill
        psg = self._is_psg if solo is None else solo.kind == ChannelType.PSG

        # A Cxx due on the attack row gives way to EDx when the note lasts into the next row:
        # the volume is then set there (_attack_commands).  Sonic 1's Drowning FM4 pans every
        # other note hard, so half its notes carry a -3 dB Cxx, and all of them start a tick off
        # the grid.
        # A sound inside a sample bank (core.merge.banks): the note starts with 9xx at its offset, so
        # the attack row's effect slot is the offset's
        self._ring.member = (plan.bank_members.get((self._cfg.source, tick))
                             if plan is not None and solo is None else None)
        region = (plan.region_at(self._cfg.source, tick)
                  if self._ring.member is not None and plan is not None else None)
        return NoteOn(event=event, res=res, tick=tick, duration=note.duration, instrument=res.instrument,
                      mod_note=ModNote(self._router.note(tick, res.index)), solo=solo,
                      vib_on=self._modulation.active and res.path != "merged", fill=fill, psg=psg,
                      cut_tick=self._cut_tick(event, fill, psg), region=region,
                      bank9=region is not None and region[0] > 0)

    def _cut_tick(self, event, fill: int, psg: bool) -> float | None:
        """Where the note is cut, as the attack row's slot needs to know (ECx when inside it): its
        note fill, else a PSG note's end of duration."""
        note, tick = event.note, event.tick_position
        fill_t = fill * self._timeline.ticks_per_frame_at(tick)
        if fill > 0 and fill_t < note.duration:
            return tick + fill_t
        if psg:
            return tick + self._ring_ticks.get(id(event), note.duration)
        return None

    def _resolve_legato(self, n: NoteOn) -> None:
        """smpsNoAttack before a note byte: the driver writes the new frequency and skips the
        key-on (a grace note bending into the chord, Sonic 1's Drowning slides).  A MOD note re-triggers
        its sample, so the note is written with a tone portamento at full speed instead (3FF:
        the period slides in a tick, no re-trigger; the instrument number only resets the
        volume).  It needs the effect slot, so no EDx, and a Cxx due moves to the next row."""
        n.legato = self._legato_allowed(n)
        ring = self._ring
        if not n.legato or self._ctx.legato_mode != "strict":
            return
        if ring.inst is None or ring.chip is None or ring.inst == n.instrument:
            return

        # The target lies in another range of the voice (another instrument, its sample
        # rendered for another octave).  A portamento never changes the sample, so the slide
        # is written on the one that is sounding: the same chip pitch, as many semitones from
        # the previous MOD note as it is from the previous chip pitch (Sonic 1's Green Hill Zone
        # FM3 grace C6 -> B5 crosses voice $08's C6 range boundary and landed an octave up).  Off the
        # MOD's three octaves, the note is re-triggered on its own instrument instead.
        shifted = ring.index + (n.res.chip - ring.chip)
        if 0 <= shifted <= ModNote.B3.value:
            n.instrument, n.mod_note = ring.inst, ModNote(shifted)
        else:
            n.legato = False

    def _legato_allowed(self, n: NoteOn) -> bool:
        """Whether a no-attack note may slide (settings.yaml `legato`): never under `retrigger`
        or into a banked sound; under `strict` only onto a sample of the same voice."""
        mode = self._ctx.legato_mode
        if not n.event.note.is_no_attack or n.res.path == "merged" or mode == "retrigger":
            return False
        if n.region is not None:
            return False            # a banked sound starts at its offset: a note-on
        if mode != "strict":
            return True

        # Nothing has sounded on this channel yet: a portamento would never trigger a sample
        # (Sonic 1's Drowning FM3 trill is no-attack from its first note; the hardware plays it).
        if self._ring.inst is None:
            return False

        # smpsSetvoice between the notes: the hardware rewrites the operators under the
        # running envelope, so the note sounds with the new voice.  A portamento would keep
        # the old voice's sample; re-trigger on the new one (Sonic 1's Green Hill Zone FM4/FM5 at
        # the loop label: voice $08 -> $05).
        return self._ring.voice is None or self._st.voice == self._ring.voice

    # --- a melodic note's cut -------------------------------------------------------------------
    def _place_fill(self, n: NoteOn) -> FillCut:
        """Note fill: silence the channel when the driver fires PSGNoteOff/FMNoteOff.  The fill
        byte counts V-int FRAMES (it is decremented on TempoWait frames too), so it is scaled
        onto the tick timeline first.  Skipped when the fill outlasts the note: DurationTimeout
        expires first and the fill timer never completes (note sustains)."""
        out = FillCut()
        fill_ticks = n.fill * self._timeline.ticks_per_frame_at(n.tick)
        if not (n.fill > 0 and fill_ticks < n.duration):
            return out

        # Work in absolute MOD ticks (rows × speed) so the cut keeps its sub-row position: a
        # whole row → C00 on that row, otherwise ECx.  At or past the row of the next event, the
        # next note / rest takes over.
        speed = self._config.target_speed
        next_row = Cells.row_total(*self._timeline.pattern_row(n.tick + n.duration))
        fill_abs = self._fill_abs(n, fill_ticks)
        fill_row_total, fill_sub = divmod(fill_abs, speed)
        if fill_abs >= next_row * speed or fill_row_total >= self._cells.end:
            return out

        out.pattern, out.row = Cells.cell(fill_row_total)
        rate = self._release_rate(n.instrument, n.tick)
        if rate is not None and not (out.cell == n.cell and (n.delay or n.legato)):
            # The fill is a key-off: the voice releases from that row on (the sub-row position
            # is given up for the slide's slot)
            volume = round(self._fades.volume_at(n, fill_row_total * self._timeline.ticks_per_row))
            out.slides.update(self._fades.release(self._col, fill_row_total, volume, rate, n.tick,
                                                  min(self._next_note_row(n.tick), next_row)))
        else:
            self._cells.cut(self._col, fill_row_total, fill_sub)
        out.placed = True
        # ECx (or the slide's first A0y) on the attack row leaves no room for Cxx / 4xy there.
        out.slot_used = n.cell in out.slides or (out.cell == n.cell and rate is None)
        return out

    def _fill_abs(self, n: NoteOn, fill_ticks: float) -> int:
        """The MOD tick (rows × speed) a fill cuts the note at: after its start, and past the
        attack row when that row's slot holds a Cxx / 9xx (effect priority: volume beats note
        cut)."""
        speed = self._config.target_speed
        row_abs = n.row_total * speed
        fill_abs = max(row_abs + n.delay + 1, round((n.tick + fill_ticks) * speed / self._timeline.ticks_per_row))
        if fill_abs < row_abs + speed and (n.needs_cxx or n.bank9):
            fill_abs = row_abs + speed
        return fill_abs

    def _place_duration_cut(self, n: NoteOn) -> None:
        """PSG auto note-cut: silence at the note's natural end when no smpsNoteFill was placed.
        Mirrors hardware PSGDoNext setting vol=15 when the duration timer expires - after the
        smpsNoAttack continuations, which do not re-key (_ring_ticks)."""
        cut_tick = n.tick + self._ring_ticks.get(id(n.event), n.duration)
        cut_cell = self._timeline.pattern_row(cut_tick)

        # The attack row's slot holds a 9xx: a cut inside it waits for the next row
        if cut_cell == n.cell and n.bank9:
            cut_cell = Cells.cell(n.row_total + 1)

        # Another row: C00 only where no note-on fires (rest events also emit C00 there, which
        # is idempotent)
        if cut_cell != n.cell:
            pattern, row = cut_cell
            if cut_cell not in self._note_on_positions and self._cells.in_song(pattern):
                self._cells.put(pattern, row, self._col, 0xC, 0)
            return

        # Sub-row cut: note ends within the same MOD row → ECx
        ec_val = round((cut_tick - n.tick) * self._config.target_speed / self._timeline.ticks_per_row)
        ec_val = min(ec_val, self._config.target_speed - 1)
        if ec_val > 0:
            self._cells.put(n.pattern, n.row, self._col, 0xE, 0xC0 | ec_val)

    # --- the attack row -------------------------------------------------------------------------
    def _attack_commands(self, n: NoteOn, slot_used: bool) -> tuple[tuple[int, int] | None, bool]:
        """EDx, 3FF or 9xx on the attack row, and the Cxx they displace moved to the note's first
        later row with a free slot.  Returns (that Cxx's cell, whether the attack row's slot is
        taken).  A delayed note spends its slot on EDx (an in-row ECx was ruled out by
        Cells.note_cell; an attack-row 4xy is given up — the later rows carry it)."""
        if n.delay:
            if slot_used:          # cannot happen; keep the cut if it does
                n.delay = 0
            else:
                self._cells.put(n.pattern, n.row, self._col, 0xE, 0xD0 | n.delay)
                slot_used = True
        if n.legato and not slot_used:
            self._cells.put(n.pattern, n.row, self._col, 0x3, 0xFF)
            slot_used = True
        if n.bank9:
            assert n.region is not None
            self._cells.put(n.pattern, n.row, self._col, 0x9, n.region[0] >> 8)
            slot_used = True
            self._ctx.stats.bank_cxx_moved += n.needs_cxx
        if not ((n.delay or n.legato or n.bank9) and n.needs_cxx):
            return None, slot_used
        return self._move_cxx(n), slot_used

    def _move_cxx(self, n: NoteOn) -> tuple[int, int] | None:
        """The attack row's Cxx on the first later row of the note whose slot is free (a cut placed
        before keeps its row) → that cell.  One row at the instrument's own level, then the right
        one; a lost row of level beats 33 ms of timing."""
        end_row = Cells.row_total(*self._timeline.pattern_row(n.tick + n.duration))
        for r in range(n.row_total + 1, min(end_row, self._cells.end)):
            pattern, row = Cells.cell(r)
            if not self._cells.free(pattern, row, self._col):
                continue
            self._cells.put(pattern, row, self._col, 0xC, n.volume)
            return pattern, row
        return None

    # --- after the attack -----------------------------------------------------------------------
    def _write_decay(self, n: NoteOn, fill: FillCut) -> None:
        """A sliding loop's fall (Fades.decay), up to the row the ring ends on, the next note-on,
        or the fill; the ring's volume is where it fell to."""
        ring_end = n.tick + self._ring_ticks.get(id(n.event), n.duration)
        stop = min(Cells.row_total(*self._timeline.pattern_row(ring_end)), self._next_note_row(n.tick),
                   self._cells.end)
        if fill.placed:
            stop = min(stop, Cells.row_total(*fill.cell))
        fallen = self._fades.decay(n, self._col, stop)
        if fallen is not None:
            self._ring.volume = fallen

    def _cut_banked(self, n: NoteOn) -> None:
        """A banked sound would run on into the next one in its bank: cut once it is over, unless
        the channel's next note comes first (a looped sound is its bank's last and needs none)."""
        member = self._ring.member
        if n.region is None or member is None or member.looped:
            return
        self._cut_bank_sound(n.tick, n.mod_note.value, n.region[1])

    def _cut_bank_sound(self, tick: int, note_value: int, sound_bytes: int) -> None:
        """Cut a banked sound where its bytes run out at the note's rate, unless the channel's next
        note comes first."""
        rate = note_rate(note_value, self._ctx.amiga_clock)
        self._ctx.stats.bank_cuts += self._cells.cut_after(self._col, tick, sound_bytes / rate,
                                                           self._next_note_row(tick))

    # --- ties -----------------------------------------------------------------------------------
    def _retune_tie(self, tick: int) -> None:
        """A tie sounding at a detune the note-on did not: E1x / E2x on the tie's row."""
        ring = self._ring
        assert ring.cents is not None and ring.chip is not None
        pattern, row = self._timeline.pattern_row(tick)
        col = self._router.current(tick)
        if not self._cells.in_song(pattern):
            return
        if (pattern, row) == ring.cell or self._router.borrowed(col, tick):
            return                      # the attack row's slide would retune the attack too

        want = detune_cents(ring.chip, self._st.detune, self._ctx.song.rules.fm_frequencies) - ring.cents
        moved = self._cells.fine_slide(pattern, row, col, ring.period, want)
        if moved is None:
            return
        ring.period -= moved
        ring.cents += 1200.0 * math.log2((ring.period + moved) / ring.period)

    # --- shared ---------------------------------------------------------------------------------
    def _emit_volume(self, inst: int) -> int:
        """MOD volume for a note on `inst` now, by the sounding note's chord gain and bank."""
        return self._levels.emit(self._st, inst, self._ring.gain_db, self._ring.member)

    def _release_rate(self, inst: int | None, tick: int) -> float | None:
        return self._fades.release_rate(inst, self._ring.member, tick)

    def _next_note_row(self, tick: int) -> int:
        """The first row (over the whole song) the next note-on after `tick` can land on."""
        i = bisect.bisect_right(self._note_on_ticks, tick)
        if i >= len(self._note_on_ticks):
            return self._cells.end
        return int(self._note_on_ticks[i] // self._timeline.ticks_per_row)
