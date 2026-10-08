"""One SMPS channel written into the MOD's cells: notes, rests, and the commands that carry the
driver's behaviour a MOD note does not have by itself.

    walk_channel ──► effect  → MOD-emission state (note fill, vibrato, level accumulator)
                ├──► folded  → this channel's note plays elsewhere: what rang here ends
                ├──► rest    → C00, or a release slide (A0y rows)
                ├──► DAC     → the drum's sample at its note (9xx inside a sample bank)
                └──► melodic → note-on, then on its rows: ECx / C00 (fill, PSG duration),
                               EDx (between rows), 3FF (legato), 9xx (bank), Cxx (level),
                               4xy (vibrato), E1x / E2x (a tie's detune), A0y / 6xy (a
                               sliding loop's fall, loop_decay: slide)

Effect priority, one per row: Cxx > 4xy > ECx > A0y; EDx, 3FF and 9xx take the attack row and move
a Cxx due there to the next free row of the note.  A fall's slide rides a 4xy row as 6xy (vibrato
continues) once an earlier row set that 4xy.
"""

import bisect
import math
from dataclasses import dataclass, field

from ..audio import db_to_gain
from ..config import SAMPLE_SLOT, SAMPLE_VOLUME, ChannelConfig, ConversionConfig, SynthesisSettings
from ..diagnostics import Diagnostics, WarningKind
from ..merge import Composite, MergePlan
from ..mod import MOD_MAX_VOLUME, MOD_NOTE_MAP, PERIOD_TABLE, ModFile, ModNote, clamp_mod_volume, note_rate
from ..plan import DetunePlan, DriverState, ResolvedNote, Timeline, detune_cents, fm_catalogue, walk_channel
from ..smps import CoordFlag, SmpsChannel, SmpsSong
from ..smps import semitone_to_note_name as _semitone_to_name
from .level_plan import fm_tl_to_mod, psg_att_to_mod
from .vibrato import VibratoSpeed, vibrato_depth

_FINE_SLIDE_MAX = 0xF          # E1x / E2x move the period by at most 15 units
_MAX_RELEASE_ROWS = 64         # a release still sounding this many rows on is cut (rate 0 rings forever)
_HIGHEST_NOTE = 35             # B3, the last of the MOD's 36 notes


def _grid_rows(row_total: int, end: float, tpr: float):
    """The ticks the rows from `row_total` on start at, before `end`."""
    while row_total * tpr < end:
        yield row_total * tpr
        row_total += 1


@dataclass
class EmissionStats:
    """What the channel writers counted, for the conversion's infos."""
    bank_delays_dropped: int = 0   # banked drum notes whose EDx gave way to the 9xx offset
    bank_cuts: int = 0             # banked notes cut before the next sound in their slot
    bank_cxx_moved: int = 0        # banked melodic notes whose attack-row Cxx went to the next row
    tie_retunes: dict = field(default_factory=lambda: {'placed': 0, 'skipped': 0})   # E1x / E2x on ties


@dataclass
class WriterContext:
    """What every channel's writer shares in one conversion."""
    mod: ModFile
    config: ConversionConfig
    song: SmpsSong
    synth: SynthesisSettings | None
    timeline: Timeline
    diag: Diagnostics
    vibrato: VibratoSpeed
    merge: MergePlan | None
    detune: DetunePlan | None
    fm_volume_mode: str                     # "baked" | "absolute" | "off"
    psg_volume_mode: str                    # "baked" | "absolute"
    pan_law_db: float
    fm_baseline_db: dict[int, float]        # baked levels: what each sample_list volume stands for
    psg_baseline_db: dict[int, float]
    release: dict[int, float | None]        # {instrument: release rate dB/s}
    release_slides: bool                    # end FM notes with a volume slide instead of C00
    player: str                             # settings.yaml `player`: the 4xy depth table
    leading_rests: dict[int, str]           # MOD channel -> source; laid out by ModLayout.leading_rests
    stats: EmissionStats
    # {instrument: (sample index its level starts to fall at, dB per sample)}: a sliding sustain
    # loop's fall (loop_decay: slide), which _write_decay writes into each note as volume slides
    decay: dict[int, tuple[int, float]] = field(default_factory=dict)
    # {instrument: the sources whose note-ons play it} (core/convert/sample_names.py)
    played: dict[int, set] = field(default_factory=dict)
    _sample_detunes: dict[int, float] | None = field(default=None, init=False)

    @property
    def amiga_clock(self) -> float:
        return self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock

    @property
    def legato_mode(self) -> str:
        return self.synth.legato if self.synth else "strict"

    def sample_cents(self, inst: int) -> float:
        """Cents an FM instrument's sample is detuned by (core.plan.detune): its FNUM offset at the
        pitch it is rendered at."""
        if self._sample_detunes is None:
            self._sample_detunes = {i.inst: detune_cents(i.synth_idx + 12, i.layers[0].fnum_offset,
                                                         self.song.fm_frequencies)
                                    for i in fm_catalogue(self.song, self.config).instruments.values()
                                    if len(i.layers) == 1}
        return self._sample_detunes.get(inst, 0.0)


class _ColumnRouter:
    """Where one channel's notes go in the merged build (core.merge.MergePlan): its own column,
    or the one a merge_patterns group routes it to in the note's pattern; which of its notes
    play on another channel; what a drum hit plays there.  Without a plan: its own column,
    every note, as written.

        pattern:   1-4        5-c        d-10
        FM5     →  col 1      (folded)   col 4    ← mod_channel / fill routes
    """

    def __init__(self, ctx: WriterContext, source: str, home: int):
        self._mod, self._timeline, self._plan = ctx.mod, ctx.timeline, ctx.merge
        self._source, self._home = source, home
        self._last: int | None = None      # the column the previous note-on went to
        self._away = self._plan.away_patterns(source) if self._plan is not None else frozenset()

    def _pattern(self, tick: int) -> int:
        return self._timeline.pattern_of(tick)

    def column_for(self, tick: int) -> int:
        """The column a note-on at `tick` takes (the reference build's pattern, as the groups
        count them)."""
        if self._plan is None:
            return self._home
        r = self._plan.route_at(self._source, self._pattern(tick))
        return self._home if r is None else r

    def current(self, tick: int) -> int:
        """The column this channel's ring is on: the last note-on's."""
        return self._last if self._last is not None else self.column_for(tick)

    def take(self, pattern: int, row: int, tick: int, sounding: bool) -> int:
        """The column a note-on at `tick` goes to; a note still ringing on another column
        (`sounding`, the previous block's) is cut there, as the re-key ended it."""
        chan = self.column_for(tick)
        last = self._last
        if last is not None and last != chan and sounding and not self._mod.note_at(pattern, row, last):
            self._mod.set_cursor(pattern, last, row)
            self._mod.set_effect(0xC, 0)
        self._last = chan
        return chan

    def plays_here(self, ev) -> bool:
        """A note-on this channel's output sounds: not one folded onto another channel (or
        dropped) in its pattern."""
        return (self._plan is None or getattr(ev, "merged", None) is not None
                or not self._plan.is_folded(self._source, ev.tick_position))

    def away(self, ev) -> bool:
        """An own event in a pattern this channel plays nothing of its own in (a follower's, or
        dropped there)."""
        return (bool(self._away) and getattr(ev, "merged", None) is None
                and self._pattern(ev.tick_position) in self._away)

    def borrowed(self, column: int, tick: int) -> bool:
        """Another channel's notes take `column` at `tick` (mod_channel).  A group routing this
        channel's own notes there is not a borrow: its rests release on their own column."""
        if self._plan is None:
            return False
        owner = self._plan.routed_into(column, self._pattern(tick))
        return owner is not None and owner != self._source

    def note(self, tick: int, index: int) -> int:
        """The MOD note a primary note at `tick` is triggered at: a transposed mix's own."""
        return index if self._plan is None else self._plan.note_at(self._source, tick, index)

    def drum(self, tick: int, inst: int, note: ModNote) -> tuple[int, ModNote, tuple[int, int] | None]:
        """(instrument, note, bank region) a drum hit plays: a composite with its hi-hat folded
        in, and where its sound starts in a sample bank."""
        if self._plan is None:
            return inst, note, None
        return (self._plan.instrument_at(self._source, tick, inst),
                ModNote(self._plan.note_at(self._source, tick, note.value)),
                self._plan.region_at(self._source, tick))


@dataclass
class _Note:
    """One melodic note-on while it is being written (ChannelWriter._on_melodic)."""
    event: object
    res: ResolvedNote
    tick: int
    duration: int
    instrument: int
    mod_note: ModNote
    solo: object                        # the NoteOn core.merge spliced it from, else None
    vib_on: bool                        # this channel's modulation applies
    fill: int                           # smpsNoteFill frames in force
    psg: bool                           # a PSG note: it ends at its duration
    cut_tick: float | None              # where a fill / duration cut falls
    region: tuple[int, int] | None      # (offset, bytes) of its sound inside a sample bank
    bank9: bool                         # starts with 9xx at that offset
    legato: bool = False                # written as a 3FF portamento, not a note-on
    needs_cxx: bool = False             # its level differs from its instrument's (once legato chose it)
    pattern: int = 0
    row: int = 0
    delay: int = 0                      # EDx, MOD ticks


@dataclass
class _Fill:
    """Where a note's fill cut went (ChannelWriter._place_fill)."""
    placed: bool = False
    slot_used: bool = False             # the cut (ECx) or the slide's first A0y took the attack row
    pattern: int = -1
    row: int = -1
    slides: set = field(default_factory=set)    # rows a release slide took


class ChannelWriter:
    def __init__(self, ctx: WriterContext, channel: SmpsChannel, chan_cfg: ChannelConfig, is_dac: bool):
        self._ctx = ctx
        self._mod = ctx.mod
        self._config = ctx.config
        self._timeline = ctx.timeline
        self._channel = channel
        self._cfg = chan_cfg
        self._is_dac = is_dac
        self._is_psg = chan_cfg.source.startswith('PSG')
        self._col = chan_cfg.mod_channel           # the column the last note-on or rest wrote to
        self._router = _ColumnRouter(ctx, chan_cfg.source, chan_cfg.mod_channel)

        # Driver state: level, pan, transpose, FM voice and the active PSG entry — including
        # the smpsHeaderPSG voice.  Shared with the level pre-passes and the rate-3 derivation
        # so the four passes cannot disagree about what a note plays.
        self._st = DriverState.for_channel(channel, ctx.config, chan_cfg.instrument)

        # MOD-emission state, which the driver knows nothing about
        self._note_fill = 0
        self._vibrato_active = False
        self._vibrato_speed = 0
        self._vibrato_change = 0   # raw SMPS delta byte (FNUM / PSG divider units); scaled per note
        self._vibrato_steps = 0    # raw SMPS steps byte
        self._vibrato_wait = 0     # ticks to delay before vibrato starts
        self._range_entry = None   # voice_map InstrumentRange matched on most recent note

        self._dac_map = {dac_cfg.name: dac_cfg for dac_cfg in ctx.config.dac_samples}
        # {inst_num: sample volume} from sample_list, for Cxx scaling
        self._sample_vols = {e[SAMPLE_SLOT]: (e[SAMPLE_VOLUME] if len(e) > SAMPLE_VOLUME else MOD_MAX_VOLUME) for e in ctx.config.sample_list or []}

        # How the level st tracks reaches the MOD — see SynthesisSettings.fm_volume_mode.
        # "baked" needs no accumulator (the note's level is read off st at placement time);
        # the other two carry current_volume, a MOD volume in its own right.  The FM law applies
        # to every FM note on this channel, its own or one spliced in from another channel
        # (core.merge: a solo or pool note on the drum channel keeps its level).
        fm_mode = ctx.fm_volume_mode
        self._fm_absolute = fm_mode == "absolute"
        self._fm_baked = fm_mode == "baked"
        self._psg_baked = ctx.psg_volume_mode == "baked"
        self._current_volume = chan_cfg.volume
        if is_dac or (not self._is_psg and fm_mode == "off"):
            self._st.tl = 0          # neither mode reads the smpsHeaderFM volume byte
        if self._is_psg or (self._fm_absolute and not is_dac):
            self._current_volume = self._level_volume()

        # The sounding note, as _emit_volume and _release_rate read it: dB a unison chord adds
        # to it (ResolvedNote.gain_db), and the banked composite it plays (core.merge.banks, else
        # None) - measured under its own id, released at its primary's rate
        self._note_gain = 0.0
        self._bank_member: Composite | None = None

        self._ring_ticks = self._collect_ring_ticks()
        # PSG auto note-cut: hardware PSGDoNext sets vol=15 when note duration expires.  The
        # note-on (pattern, row) positions, so no C00 is placed where a later set_note writes a
        # note (set_note retains effect bytes, so a pre-placed C00 would silence the trigger).
        self._note_on_positions = self._note_on_cells() if self._is_psg else set()
        # Every note-on tick of this channel (spliced notes included): a release slide runs up to
        # the row before the next one, so it never lands in a note-on's cell
        self._note_on_ticks = sorted(ev.tick_position for ev in self._note_ons())

        self._last_note_cell: tuple[int, int] | None = None   # where this channel's previous note-on went
        self._last_inst: int | None = None                    # the instrument the previous note-on played ...
        self._last_vol = MOD_MAX_VOLUME                       # ... and the MOD volume it played at
        self._last_idx = 0                                    # ... its MOD note ...
        self._last_chip: int | None = None                    # ... and the chip pitch that note sounds
        self._last_voice: int | None = None                   # ... and the FM voice it was played with
        # The detune a tie (smpsNoAttack + a duration) sounds at: the driver writes the frequency
        # with the track's Detune on a tie too, so Scrap Brain FM4's scoop (`smpsAlterNote $EC`,
        # `nG5, $02`, `smpsAlterNote $00`, `smpsNoAttack, $06`) rises 49 c on its tie.  The MOD
        # note keeps its detune variant's sample there: an E1x / E2x moves its period instead.
        self._sounding_cents: float | None = None             # cents the sounding FM note plays off its table pitch
        self._sounding_period = 0                             # ... and its MOD period, slides included

    def _note_ons(self):
        """The note-on events this channel's output sounds (spliced ones included)."""
        return (ev for ev in self._channel.events
                if ev.is_note and not ev.note.is_rest and self._router.plays_here(ev))

    def _note_on_cells(self) -> set[tuple[int, int]]:
        """(pattern, row) of every note-on: the row it rounds to and the row it starts in."""
        tpr = self._timeline.ticks_per_row
        cells: set[tuple[int, int]] = set()
        for ev in self._note_ons():
            cells.add(self._timeline.pattern_row(ev.tick_position))
            cells.add(divmod(int(ev.tick_position // tpr), 64))
        return cells

    def _collect_ring_ticks(self) -> dict[int, int]:
        """How long each note-on rings before a PSG duration cut: its own duration plus the
        smpsNoAttack continuations after it (`nE5, $34, smpsNoAttack, $34` holds 104 ticks on
        the hardware; Green Hill's last verse chord cut its chime at 52)."""
        ring_ticks: dict[int, int] = {}
        ringing = None
        for ev in self._channel.events:
            if not ev.is_note:
                continue
            if ev.note.is_rest and ev.note.is_no_attack and ringing is not None:
                ring_ticks[id(ringing)] += ev.note.duration
                continue
            ringing = None if ev.note.is_rest else ev
            if ringing is not None:
                ring_ticks[id(ev)] = ev.note.duration
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
                return
        self._on_stop()

    def _on_stop(self) -> None:
        """smpsStop keys the track off (StopTrack → FMNoteOff): an FM note still ringing at the
        track's end releases there, as a rest would end it.  Stage Clear's closing chord held
        its looped samples through the end and into the song's restart.  A channel that loops
        never stops; the DAC plays its sample out, and a PSG note is cut at its duration."""
        if self._channel.has_jump or self._is_dac or self._is_psg or self._last_inst is None:
            return
        notes = [ev for ev in self._channel.events if ev.is_note]
        if not notes:
            return
        last = notes[-1]
        if last.note.is_rest and not last.note.is_no_attack:
            return                      # keyed off already
        if not last.note.is_rest and not self._router.plays_here(last):
            return                      # folded onto another channel: it ends there
        end = last.tick_position + last.note.duration
        pattern, row = self._timeline.pattern_row(end)
        if pattern >= self._config.max_patterns:
            return
        self._col = self._router.current(end)
        self._mod.ensure_pattern(pattern)       # an end on a pattern's first row: none written yet
        if not self._mod.note_at(pattern, row, self._col):
            self._key_off(pattern, row, end)

    def _on_effect(self, event) -> None:
        """walk_channel has advanced st past this flag: level, pan, transpose, FM voice and PSG
        instrument routing.  What is left is MOD-emission state.  smpsSetvoice, smpsPan,
        smpsChangeTransposition, smpsPSGform and smpsPSGvoice are st.apply's business; smpsNop
        has no MOD equivalent; smpsAlterNote is a raw FNUM offset (~10 cents) that does not
        affect note pitch or voice_map lookup."""
        eff = event.effect
        kind = eff.flag
        if kind in (CoordFlag.ALTER_VOL, CoordFlag.SET_VOL):
            # st.apply moved the TL offset / attenuation; the non-baked modes keep their own
            # MOD-volume accumulator on top of it: the channel volume less the TL steps the song
            # moved from its header volume (smpsAlterVol: by its delta; SET_VOL: to its level)
            if self._is_psg or self._fm_absolute:
                self._current_volume = self._level_volume()
            elif not self._fm_baked:
                moved = self._current_volume - eff.params[0] if kind == CoordFlag.ALTER_VOL else                     self._cfg.volume - (eff.params[0] - self._channel.header.volume)
                self._current_volume = max(0, min(64, moved))
        elif kind == CoordFlag.NOTE_FILL:
            self._note_fill = eff.params[0]
        elif kind == CoordFlag.MOD_SET:
            # wait, speed, change, steps
            self._vibrato_wait = eff.params[0]
            self._vibrato_change = eff.params[2]   # raw delta; scaled to period units at placement
            self._vibrato_steps = eff.params[3]
            self._vibrato_speed = self._ctx.vibrato.speed(eff.params[1], self._vibrato_steps, self._cfg.source,
                                                          event.tick_position)
            self._vibrato_active = True
        elif kind == CoordFlag.MOD_ON:
            self._vibrato_active = True
        elif kind == CoordFlag.MOD_OFF:
            self._vibrato_active = False

    def _on_folded(self, tick: int) -> None:
        """This note plays on its group's primary channel in this pattern (merge_patterns), or
        nowhere (dropped).  What still rings here from a pattern the channel was live in ends
        now, as the re-key ended it on the hardware; the channel's cells stay empty otherwise."""
        if self._last_inst is None:
            return
        pattern, row = self._timeline.pattern_row(tick)
        if pattern >= self._config.max_patterns:
            return
        self._col = self._router.current(tick)
        if not self._mod.note_at(pattern, row, self._col):      # else a note-on already takes the column
            self._key_off(pattern, row, tick)
        self._last_inst = None

    def _on_rest(self, event) -> None:
        note, tick = event.note, event.tick_position

        # is_no_attack=True marks an FM/DAC standalone-duration continuation — the YM2612
        # envelope sustains naturally; do not emit C00.
        if note.is_no_attack:
            if self._sounding_cents is not None and self._last_inst is not None and not self._st.is_psg:
                self._retune_tie(tick)
            return

        pattern, row = self._timeline.pattern_row(tick)
        if pattern >= self._config.max_patterns:
            return
        if self._last_inst is None and self._router.away(event):
            return                  # nothing of this channel's sounds here: no C00 clutter
        self._col = self._router.current(tick)
        if (pattern, row) == (0, 0):
            # A leading rest.  Its C00 matters once the song loops back to position 0, and the
            # cell may hold a tempo command, so it is placed after every channel is converted
            # (ModLayout.leading_rests).
            self._ctx.leading_rests[self._col] = self._cfg.source
            return

        # Another channel's notes take this column here (mod_channel): its note-on ends this ring
        if self._router.borrowed(self._col, tick) and self._mod.note_at(pattern, row, self._col):
            return
        self._key_off(pattern, row, tick)

    def _key_off(self, pattern: int, row: int, tick: int) -> None:
        """End what rings on the column: the note fades at the voice's release rate (the sample
        loops, or would be cut short of the chip's release either way), else C00.  No slide on
        a column another channel's notes take (mod_channel): it would sit on their notes."""
        rate = None if self._router.borrowed(self._col, tick) else self._release_rate(self._last_inst, tick)
        if rate is not None:
            self._write_release(self._col, pattern * 64 + row, self._last_vol, rate, tick, self._next_note_row(tick))
            return
        self._mod.set_cursor(pattern, self._col, row)
        self._mod.set_effect(0xC, 0)

    def _on_note(self, event, res: ResolvedNote | None) -> bool:
        """A note-on; False once the song runs past max_patterns (the channel stops)."""
        pattern, row = self._timeline.pattern_row(event.tick_position)
        if pattern >= self._config.max_patterns:
            self._ctx.diag.warn(WarningKind.PATTERN_OVERFLOW, channel=self._cfg.source, pattern=pattern,
                                max=self._config.max_patterns)
            return False

        self._mod.set_cursor(pattern, self._col, row)
        if self._is_dac and res is None:
            return self._on_dac(event)
        assert res is not None
        return self._on_melodic(event, res)

    def _open_cell(self, pattern: int, row: int, tick: int) -> None:
        """Take the column a note-on at `tick` goes to, the cursor on its cell, a stale C00 there
        cleared."""
        self._col = self._router.take(pattern, row, tick, self._last_inst is not None)
        self._mod.set_cursor(pattern, self._col, row)
        self._clear_stale_cut(pattern, row, self._col)
        self._last_note_cell = (pattern, row)

    # --- DAC ------------------------------------------------------------------------------------
    def _on_dac(self, event) -> bool:
        """A drum hit: its dac_samples instrument and note.  DAC notes carry no other effect, so
        the slot is always free for EDx."""
        note, tick = event.note, event.tick_position
        pattern, row, note_delay = self._note_cell(tick, True, None)
        if pattern >= self._config.max_patterns:
            return False
        self._open_cell(pattern, row, tick)

        dac_cfg = self._dac_map.get(note.dac_name)
        if dac_cfg:
            note_delay = self._play_drum(pattern, row, tick, dac_cfg, note_delay)
        else:
            # Fallback: use default instrument and C3
            self._mod.set_note(ModNote.C3, self._st.instrument)
            self._last_inst, self._last_vol = self._st.instrument, MOD_MAX_VOLUME
        if note_delay:
            self._mod.set_effect(0xE, 0xD0 | note_delay)
        return True

    def _play_drum(self, pattern: int, row: int, tick: int, dac_cfg, note_delay: int) -> int:
        """The drum's instrument and note (a composite with its hi-hat folded in) on the cursor's
        cell.  Returns the EDx delay left: a sound inside a sample bank starts with 9xx instead."""
        plan = self._ctx.merge
        dac_inst, dac_note, region = self._router.drum(
            tick, dac_cfg.mod_instrument, MOD_NOTE_MAP.get(dac_cfg.mod_note, ModNote.C3))
        self._mod.set_note(dac_note, dac_inst)
        self._played(dac_inst, tick)
        self._last_inst, self._last_vol = dac_inst, self._sample_volume(dac_inst)
        self._bank_member = plan.bank_members.get((self._cfg.source, tick)) if plan is not None else None
        if region is None:
            return note_delay

        # A sound inside a sample bank (core.merge.banks): start at its offset and cut the note
        # once it is over, before the next sound in the slot
        offset, sound = region
        if offset:
            self._mod.set_effect(0x9, offset >> 8)
            if note_delay:
                note_delay = 0            # the slot holds the offset
                self._ctx.stats.bank_delays_dropped += 1
        self._cut_bank_sound(pattern, row, tick, dac_note.value, sound)
        return note_delay

    # --- melodic --------------------------------------------------------------------------------
    def _on_melodic(self, event, res: ResolvedNote) -> bool:
        """Which instrument and which MOD note (resolve_note, through walk_channel: the range
        lookup in the config's range_space, root + (key - low) for an anchored entry, the channel
        transpose otherwise), then the note-on and the commands on its rows."""
        n = self._melodic_note(event, res)
        self._resolve_legato(n)
        # After legato: a strict legato plays the sounding instrument, whose level is what counts
        n.needs_cxx = self._emit_volume(n.instrument) != self._sample_volume(n.instrument)

        # Where the note goes (see _note_cell): on its own row with an EDx delay when it starts
        # between rows and the effect slot is free
        slot_free = not n.legato and not n.bank9 and (not n.needs_cxx
                                                      or n.duration >= 2 * self._timeline.ticks_per_row)
        n.pattern, n.row, n.delay = self._note_cell(n.tick, slot_free, n.cut_tick)
        if n.pattern >= self._config.max_patterns:
            return False
        self._open_cell(n.pattern, n.row, n.tick)

        self._mod.set_note(n.mod_note, n.instrument)
        self._played(n.instrument, n.tick)
        self._last_inst, self._last_vol = n.instrument, self._emit_volume(n.instrument)
        self._last_idx, self._last_chip, self._last_voice = n.mod_note.value, n.res.chip, self._st.voice
        if self._ctx.detune is not None and not n.psg:
            self._sounding_cents = self._ctx.sample_cents(n.instrument)
            self._sounding_period = PERIOD_TABLE[n.mod_note.value]

        fill = self._place_fill(n)
        if n.psg and not fill.placed:
            self._place_duration_cut(n)
        cxx_coord, slot_used = self._attack_commands(n, fill.slot_used)
        vib_speed, vib_depth = self._vibrato_of(n)
        if not slot_used:
            self._attack_level_or_vibrato(n, vib_speed, vib_depth)
        if n.vib_on and vib_speed > 0:
            self._continue_vibrato(n, vib_speed, vib_depth, fill, cxx_coord)
        self._write_decay(n, fill)
        self._cut_banked(n)
        return True

    def _played(self, inst: int, tick: int) -> None:
        """Note which source plays `inst`: this channel, or the follower a solo note came from."""
        plan = self._ctx.merge
        solo = plan.solo.get((self._cfg.source, tick)) if plan is not None else None
        self._ctx.played.setdefault(inst, set()).add(solo[0] if solo else self._cfg.source)

    def _melodic_note(self, event, res: ResolvedNote) -> _Note:
        """The note as resolved, with what its placement depends on: its fill or duration cut,
        its sound inside a sample bank."""
        note, tick = event.note, event.tick_position
        plan = self._ctx.merge
        self._note_gain = res.gain_db
        self._range_entry = None if self._is_psg else res.entry
        self._warn_resolution(res, note)

        # A solo note carries none of this channel's modulation, but its own note fill (the
        # follower's smpsNoteFill, on the NoteOn core.merge spliced it from), and a PSG note
        # ends at its duration wherever it plays (an FM one on a PSG channel does not: it rings
        # into its rest's release)
        solo = getattr(event, "merged", None)
        fill = self._note_fill if solo is None else solo.fill
        psg = self._is_psg if solo is None else solo.kind == "PSG"

        # A Cxx due on the attack row gives way to EDx when the note lasts into the next row:
        # the volume is then set there (_attack_commands).  Drowning FM4 pans every other note
        # hard, so half its notes carry a -3 dB Cxx, and all of them start a tick off the grid.
        # A sound inside a sample bank (core.merge.banks): the note starts with 9xx at its offset, so
        # the attack row's effect slot is the offset's
        self._bank_member = (plan.bank_members.get((self._cfg.source, tick))
                             if plan is not None and solo is None else None)
        region = (plan.region_at(self._cfg.source, tick)
                  if self._bank_member is not None and plan is not None else None)
        return _Note(event=event, res=res, tick=tick, duration=note.duration, instrument=res.instrument,
                     mod_note=ModNote(self._router.note(tick, res.index)), solo=solo,
                     vib_on=self._vibrato_active and res.path != "merged", fill=fill, psg=psg,
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

    def _resolve_legato(self, n: _Note) -> None:
        """smpsNoAttack before a note byte: the driver writes the new frequency and skips the
        key-on (a grace note bending into the chord, Drowning's slides).  A MOD note re-triggers
        its sample, so the note is written with a tone portamento at full speed instead (3FF:
        the period slides in a tick, no re-trigger; the instrument number only resets the
        volume).  It needs the effect slot, so no EDx, and a Cxx due moves to the next row."""
        n.legato = self._legato_allowed(n)
        if not n.legato or self._ctx.legato_mode != "strict":
            return
        if self._last_inst is None or self._last_chip is None or self._last_inst == n.instrument:
            return

        # The target lies in another range of the voice (another instrument, its sample
        # rendered for another octave).  A portamento never changes the sample, so the slide
        # is written on the one that is sounding: the same chip pitch, as many semitones from
        # the previous MOD note as it is from the previous chip pitch (Green Hill's FM3 grace
        # C6 -> B5 crosses voice $08's C6 range boundary and landed an octave up).  Off the
        # MOD's three octaves, the note is re-triggered on its own instrument instead.
        shifted = self._last_idx + (n.res.chip - self._last_chip)
        if 0 <= shifted <= _HIGHEST_NOTE:
            n.instrument, n.mod_note = self._last_inst, ModNote(shifted)
        else:
            n.legato = False

    def _legato_allowed(self, n: _Note) -> bool:
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
        # (Drowning's FM3 trill is no-attack from its first note; the hardware plays it).
        if self._last_inst is None:
            return False

        # smpsSetvoice between the notes: the hardware rewrites the operators under the
        # running envelope, so the note sounds with the new voice.  A portamento would keep
        # the old voice's sample; re-trigger on the new one (Green Hill's FM4/FM5 at the loop
        # label: voice $08 -> $05).
        return self._last_voice is None or self._st.voice == self._last_voice

    def _place_fill(self, n: _Note) -> _Fill:
        """Note fill: silence the channel when the driver fires PSGNoteOff/FMNoteOff.  The fill
        byte counts V-int FRAMES (it is decremented on TempoWait frames too), so it is scaled
        onto the tick timeline first.  Skipped when the fill outlasts the note: DurationTimeout
        expires first and the fill timer never completes (note sustains)."""
        out = _Fill()
        fill_ticks = n.fill * self._timeline.ticks_per_frame_at(n.tick)
        if not (n.fill > 0 and fill_ticks < n.duration):
            return out

        # Work in absolute MOD ticks (rows × speed) so the cut keeps its sub-row position: a
        # whole row → C00 on that row, otherwise ECx.
        speed = self._config.target_speed
        mod_chan = self._col
        next_pat, next_row = self._timeline.pattern_row(n.tick + n.duration)
        fill_abs = self._fill_abs(n, fill_ticks)
        fill_row_total, fill_sub = divmod(fill_abs, speed)
        # At or past the row of the next event, the next note / rest takes over.
        if not (fill_abs < (next_pat * 64 + next_row) * speed and fill_row_total // 64 < self._config.max_patterns):
            return out

        out.pattern, out.row = fill_row_total // 64, fill_row_total % 64
        rel_rate = self._release_rate(n.instrument, n.tick)
        if rel_rate is not None and not ((out.pattern, out.row) == (n.pattern, n.row) and (n.delay or n.legato)):
            # The fill is a key-off: the voice releases from that row on (the sub-row position
            # is given up for the slide's slot)
            out.slides.update(self._write_release(
                mod_chan, fill_row_total, round(self._decayed_volume(n, fill_row_total * self._timeline.ticks_per_row)),
                rel_rate,
                n.tick, min(self._next_note_row(n.tick), next_pat * 64 + next_row)))
        else:
            self._write_cut(mod_chan, fill_row_total, fill_sub)
        # Restore cursor to the current note's cell.
        self._mod.set_cursor(n.pattern, mod_chan, n.row)
        out.placed = True
        # ECx (or the slide's first A0y) on the attack row leaves no room for Cxx / 4xy there.
        out.slot_used = ((n.pattern, n.row) in out.slides
                         or ((out.pattern, out.row) == (n.pattern, n.row) and rel_rate is None))
        return out

    def _fill_abs(self, n: _Note, fill_ticks: float) -> int:
        """The MOD tick (rows × speed) a fill cuts the note at: after its start, and past the
        attack row when that row's slot holds a Cxx / 9xx (effect priority: volume beats note
        cut)."""
        speed = self._config.target_speed
        row_abs = (n.pattern * 64 + n.row) * speed
        fill_abs = max(row_abs + n.delay + 1, round((n.tick + fill_ticks) * speed / self._timeline.ticks_per_row))
        if fill_abs < row_abs + speed and (self._emit_volume(n.instrument) != self._sample_volume(n.instrument)
                                           or n.bank9):
            fill_abs = row_abs + speed
        return fill_abs

    def _place_duration_cut(self, n: _Note) -> None:
        """PSG auto note-cut: silence at the note's natural end when no smpsNoteFill was placed.
        Mirrors hardware PSGDoNext setting vol=15 when the duration timer expires - after the
        smpsNoAttack continuations, which do not re-key (_ring_ticks)."""
        cut_tick = n.tick + self._ring_ticks.get(id(n.event), n.duration)
        cut_pat, cut_row = self._timeline.pattern_row(cut_tick)
        if (cut_pat, cut_row) != (n.pattern, n.row):
            # Different row: write C00 only where no note-on fires (rest events also emit C00
            # there, which is idempotent)
            if (cut_pat, cut_row) not in self._note_on_positions and cut_pat < self._config.max_patterns:
                self._cut_row(cut_pat, cut_row, n)
            return

        if n.bank9:
            # The attack row's slot holds the 9xx: the cut waits for the next row
            nxt_pat, nxt_row = divmod(n.pattern * 64 + n.row + 1, 64)
            if (nxt_pat, nxt_row) not in self._note_on_positions and nxt_pat < self._config.max_patterns:
                self._mod.ensure_pattern(nxt_pat)
                self._cut_row(nxt_pat, nxt_row, n)
            return

        # Sub-row cut: note ends within the same MOD row → ECx
        ec_val = round((cut_tick - n.tick) * self._config.target_speed / self._timeline.ticks_per_row)
        ec_val = min(ec_val, self._config.target_speed - 1)
        if ec_val > 0:
            self._mod.set_effect(0xE, 0xC0 | ec_val)

    def _cut_row(self, pattern: int, row: int, n: _Note) -> None:
        """C00 on a later row of the note; the cursor goes back to its cell."""
        self._mod.set_cursor(pattern, self._col, row)
        self._mod.set_effect(0xC, 0)
        self._mod.set_cursor(n.pattern, self._col, n.row)

    def _attack_commands(self, n: _Note, slot_used: bool) -> tuple[tuple[int, int] | None, bool]:
        """EDx, 3FF or 9xx on the attack row, and the Cxx they displace moved to the note's first
        later row with a free slot.  Returns (that Cxx's cell, whether the attack row's slot is
        taken).  A delayed note spends its slot on EDx (an in-row ECx was ruled out by
        _note_cell; an attack-row 4xy is given up — the later rows carry it)."""
        if n.delay:
            if slot_used:          # cannot happen; keep the cut if it does
                n.delay = 0
            else:
                self._mod.set_effect(0xE, 0xD0 | n.delay)
                slot_used = True
        if n.legato and not slot_used:
            self._mod.set_effect(0x3, 0xFF)
            slot_used = True
        if n.bank9:
            assert n.region is not None
            self._mod.set_effect(0x9, n.region[0] >> 8)
            slot_used = True
            self._ctx.stats.bank_cxx_moved += n.needs_cxx
        if not ((n.delay or n.legato or n.bank9) and n.needs_cxx):
            return None, slot_used
        return self._move_cxx(n), slot_used

    def _move_cxx(self, n: _Note) -> tuple[int, int] | None:
        """The attack row's Cxx on the first later row of the note whose slot is free (a cut placed
        before keeps its row) → that cell.  One row at the instrument's own level, then the right
        one; a lost row of level beats 33 ms of timing."""
        mod_chan = self._col
        end_pat, end_row = self._timeline.pattern_row(n.tick + n.duration)
        end_total = min(end_pat * 64 + end_row, self._config.max_patterns * 64)
        for r_total in range(n.pattern * 64 + n.row + 1, end_total):
            p_, r_ = divmod(r_total, 64)
            self._mod.ensure_pattern(p_)
            if not self._mod.effect_slot_free(p_, r_, mod_chan):
                continue
            self._mod.set_cursor(p_, mod_chan, r_)
            self._mod.set_effect(0xC, self._emit_volume(n.instrument))
            self._mod.set_cursor(n.pattern, mod_chan, n.row)
            return p_, r_
        return None

    def _vibrato_of(self, n: _Note) -> tuple[int, int]:
        """(4xy speed, depth) for this note: a per-entry override first, else the smpsModSet's
        speed and the depth of its swing at this note's frequency word."""
        st = self._st
        override = None
        if self._range_entry is not None and self._range_entry.vibrato is not None:
            override = self._range_entry.vibrato
        elif st.psg_entry is not None and st.psg_entry.vibrato is not None:
            override = st.psg_entry.vibrato
        if override is not None:
            return (override >> 4) & 0xF, override & 0xF

        # Depth is per note: the driver's swing is a fixed number of FNUM / divider units, so its
        # size in cents depends on the chip note it is added to.
        depth = vibrato_depth(self._vibrato_change, self._vibrato_steps, PERIOD_TABLE[n.mod_note.value],
                              n.res.source + st.transpose, self._is_psg, self._ctx.player)
        return (self._vibrato_speed if depth else 0), depth

    def _attack_level_or_vibrato(self, n: _Note, vib_speed: int, vib_depth: int) -> None:
        """The attack row's free slot: Cxx when the note's level differs from its instrument's
        (MOD resets to the sample volume on each note trigger, so none is needed otherwise),
        else the 4xy when the modulation wait is over for most of the row."""
        emit_vol = self._emit_volume(n.instrument)
        if emit_vol != self._sample_volume(n.instrument):
            self._mod.set_effect(0xC, emit_vol)
            return
        wait_ticks = self._vibrato_wait * self._timeline.ticks_per_frame_at(n.tick)
        if n.vib_on and vib_speed > 0 and wait_ticks <= self._timeline.ticks_per_row / 2:
            self._mod.set_effect(0x4, (vib_speed << 4) | vib_depth)

    def _continue_vibrato(self, n: _Note, vib_speed: int, vib_depth: int, fill: _Fill,
                          cxx_coord: tuple[int, int] | None) -> None:
        """4xy on every continuation row within the note's vibrato span.  In ProTracker, 4xy only
        applies on rows where the effect is present, so it is repeated each row to get continuous
        vibrato matching SMPS modulation.  The SMPS wait is in FRAMES (DoModulation runs on
        TempoWait frames too); a row carries 4xy when modulation runs for at least half of it."""
        mod_chan = self._col
        vib_start_tick = n.tick + self._vibrato_wait * self._timeline.ticks_per_frame_at(n.tick)
        tpr = self._timeline.ticks_per_row
        fill_coord = (fill.pattern, fill.row) if fill.placed else None
        # The rows of the grid after the one the note went on, not whole rows counted from its
        # tick: a note that starts between rows (EDx) would otherwise reach a row past its end,
        # the next note's attack row (Robotnik at 3 ticks per row: a $04 triplet's 4xy started
        # the vibrato of the long note after it 200 ms early)
        row_total = n.pattern * 64 + n.row + 1
        for cont_tick in _grid_rows(row_total, n.tick + n.duration, tpr):
            if cont_tick + tpr / 2 < vib_start_tick:
                continue                    # still waiting
            cont_pat, cont_row = self._timeline.pattern_row(cont_tick)
            if cont_pat >= self._config.max_patterns:
                break
            if fill.slides and (cont_pat, cont_row) >= min(fill.slides):
                break                       # released: nothing to modulate
            if (cont_pat, cont_row) in (fill_coord, cxx_coord):
                continue
            # A song's last note rings into patterns nothing has written yet: Game Over's closing
            # G#3 (3 s of smpsModSet) had no 4xy at all while the check here stopped at them
            self._mod.ensure_pattern(cont_pat)
            self._mod.set_cursor(cont_pat, mod_chan, cont_row)
            self._mod.set_effect(0x4, (vib_speed << 4) | vib_depth)
        # Restore cursor to the attack row
        self._mod.set_cursor(n.pattern, mod_chan, n.row)

    # --- a sliding loop's fall ---------------------------------------------------------------
    def _decay_of(self, n: _Note) -> tuple[float, float] | None:
        """(seconds into the note its level starts to fall, dB per second) where the note plays a
        sliding sustain loop (loop_decay: slide), at the rate its MOD note plays the sample: a note
        above the sample's root runs through the render, and its fall, faster.  None otherwise, or
        on a sound inside a sample bank."""
        d = self._ctx.decay.get(n.instrument)
        if d is None or n.region is not None:
            return None
        flat_at, db = d
        rate = note_rate(n.mod_note.value, self._ctx.amiga_clock)
        return flat_at / rate, db * rate

    def _decayed_volume(self, n: _Note, tick: float) -> float:
        """The MOD volume a note on a sliding loop has fallen to by `tick` (its own volume without one)."""
        vol = float(self._emit_volume(n.instrument))
        d = self._decay_of(n)
        if d is None:
            return vol
        t0, db_s = d
        return vol * db_to_gain(-db_s * max(0.0, self._timeline.span_secs(n.tick, tick) - t0))

    def _write_decay(self, n: _Note, fill: _Fill) -> None:
        """A sliding loop's fall (core.audio.loops: the sample holds the level its loop starts
        at): one A0y per row of the ring toward where the render's level would be by the row's
        end, as _write_release steps a release, or EBx where the row's share is less than an A01
        takes (speed - 1 units: Game Over's bass falls one unit a row at speed 9, and A01 every
        eighth row made a staircase).  Up to the row the ring ends on (its rest's
        release takes over from the fallen volume), the next note-on, or the fill.  A row whose
        slot is taken is skipped and the next one catches up; a 4xy row becomes 6xy (vibrato
        continues + slide) once an earlier row of the note set that same 4xy; a Cxx row sets
        the volume the next rows slide from."""
        d = self._decay_of(n)
        if d is None:
            return
        tpr, speed = self._timeline.ticks_per_row, self._config.target_speed
        per_tick = speed - 1
        if per_tick < 1:
            return
        col = self._col
        ring_end = n.tick + self._ring_ticks.get(id(n.event), n.duration)
        end_pat, end_row = self._timeline.pattern_row(ring_end)
        stop = min(end_pat * 64 + end_row, self._next_note_row(n.tick), self._config.max_patterns * 64)
        if fill.placed:
            stop = min(stop, fill.pattern * 64 + fill.row)
        v = float(self._emit_volume(n.instrument))
        vib = None
        attack = self._mod.effect_at(n.pattern, n.row, col)
        if attack[0] == 0x4:
            vib = attack[1]
        for r in range(n.pattern * 64 + n.row + 1, stop):
            pattern, row = divmod(r, 64)
            self._mod.ensure_pattern(pattern)
            free = self._mod.effect_slot_free(pattern, row, col)
            eff, param = self._mod.effect_at(pattern, row, col)
            if not free and eff == 0xC:
                v = float(param)
                continue
            fall = v - self._decayed_volume(n, (r + 1) * tpr)
            y = min(15, round(fall / per_tick))
            fine = min(15, round(fall))
            if free and y < 1 and fine >= 1:
                # Less than an A01 takes (speed - 1 units): EBx, x units once, on the row's first tick
                self._mod.set_cursor(pattern, col, row)
                self._mod.set_effect(0xE, 0xB0 | fine)
                v = max(0.0, v - fine)
                continue
            if y <= 0 or not (free or (eff == 0x4 and param == vib)):
                if not free and eff == 0x4:
                    vib = param
                continue
            self._mod.set_cursor(pattern, col, row)
            self._mod.set_effect(0xA if free else 0x6, y)
            v = max(0.0, v - y * per_tick)
        if stop > n.pattern * 64 + n.row + 1:
            self._last_vol = round(v)
        self._mod.set_cursor(n.pattern, col, n.row)

    def _cut_banked(self, n: _Note) -> None:
        """A banked sound would run on into the next one in its bank: cut once it is over, unless
        the channel's next note comes first (a looped sound is its bank's last and needs none)."""
        member = self._bank_member
        if n.region is None or member is None or member.looped:
            return
        self._cut_bank_sound(n.pattern, n.row, n.tick, n.mod_note.value, n.region[1])

    def _cut_bank_sound(self, pattern: int, row: int, tick: int, note_value: int, sound_bytes: int) -> None:
        """Cut a banked sound where its bytes run out at the note's rate, unless the channel's next
        note comes first.  The cursor goes back to the note's cell."""
        rate = note_rate(note_value, self._ctx.amiga_clock)
        self._ctx.stats.bank_cuts += self._cut_after(self._col, tick, sound_bytes / rate, self._next_note_row(tick))
        self._mod.set_cursor(pattern, self._col, row)

    # --- levels ---------------------------------------------------------------------------------
    def _sample_volume(self, inst: int) -> int:
        return self._sample_vols.get(inst, MOD_MAX_VOLUME)

    def _level_volume(self) -> int:
        """The MOD volume st's level stands for outside baked mode: the PSG attenuation, or the
        FM TL offset (absolute mode)."""
        level = psg_att_to_mod(self._st.att) if self._is_psg else fm_tl_to_mod(self._st.tl)
        return round(level * self._cfg.volume / 64)

    def _emit_volume(self, inst: int) -> int:
        """MOD volume for a note on `inst` right now (equals the sample volume → no Cxx).

        Read off `st`, which is this channel's state or, for a follower's solo note on a merged
        channel, the follower's (so a PSG hat on the drum channel keeps its law).
        """
        st = self._st
        sv = self._sample_volume(inst)
        baked = self._psg_baked if st.is_psg else self._fm_baked
        if not baked:
            return round(self._current_volume * sv / 64)
        if st.is_psg and st.is_silent:
            return 0
        level = st.level_db(self._ctx.pan_law_db) + self._note_gain
        baseline = self._ctx.psg_baseline_db if st.is_psg else self._ctx.fm_baseline_db
        member = self._bank_member
        key = member.bank_id if member is not None and member.inst == inst else inst
        rel_db = level - baseline.get(key, level)
        # A banked sound's bytes carry its own volume against the bank's (sv): at its own level
        # it needs no Cxx either
        return clamp_mod_volume(sv * db_to_gain(rel_db) * self._cfg.volume / 64)

    # --- rows -----------------------------------------------------------------------------------
    def _next_note_row(self, tick: int) -> int:
        """The first row (over the whole song) the next note-on after `tick` can land on."""
        i = bisect.bisect_right(self._note_on_ticks, tick)
        if i >= len(self._note_on_ticks):
            return self._config.max_patterns * 64
        return int(self._note_on_ticks[i] // self._timeline.ticks_per_row)

    def _note_cell(self, tick: int, slot_free: bool, cut_tick: float | None) -> tuple[int, int, int]:
        """(pattern, row, EDx delay in MOD ticks) for a note-on at `tick`.

        A note that starts between two rows goes on the row it starts in, delayed by `EDx`,
        instead of being rounded to the nearer row (up to half a row early or late, and —
        Python rounds halves to even — early and late on alternate notes).  The delay needs
        the cell's one effect slot, so it is only used when the slot is free (`slot_free`:
        no `Cxx` due on the attack row that cannot move to a later row of the note) and no
        cut (`cut_tick`) falls inside the attack row.  Otherwise the note is rounded as
        before.

        Two note-ons cannot share a cell.  When the row already holds this channel's
        previous note-on (a 1-tick grace note and the note it slides into), the later one
        takes the next row undelayed: late by less than a row instead of erasing the grace.
        """
        tpr, speed = self._timeline.ticks_per_row, self._config.target_speed
        row_total = int(tick // tpr)
        # The delay is measured in FRAMES, because driver ticks are not evenly spaced: with
        # tempo modifier m, TempoWait holds every m-th frame, so tick k falls on frame
        # k + k // (m - 1).  GHZ (m = 3, 2 ticks per row): an odd tick is 1 frame = 16.7 ms
        # after its row starts, not the 25 ms an average tick lasts - exactly ED1 at speed 3.
        # A row is tpr / ticks_per_frame(m) frames and `speed` MOD ticks long.
        # (Counted from the start of the current tempo segment: smpsSetTempoMod restarts
        # the counter.)
        seg_start, m = self._timeline.segment_at(tick)
        holds = m > 1 and not self._ctx.song.header.is_sfx

        def held(k):
            return max(int(k) - seg_start, 0) // (m - 1) if holds else 0
        frames = (tick + held(tick)) - (row_total * tpr + held(row_total * tpr))
        delay = int(frames * speed * self._timeline.ticks_per_frame(m) / tpr + 0.5)
        if delay >= speed:
            row_total, delay = row_total + 1, 0
        if delay and cut_tick is not None and round(cut_tick * speed / tpr) < (row_total + 1) * speed:
            slot_free = False
        if delay and not slot_free:
            row_total, delay = round(tick / tpr), 0
        if divmod(row_total, 64) == self._last_note_cell:
            row_total, delay = row_total + 1, 0
        return row_total // 64, row_total % 64, delay

    # --- endings --------------------------------------------------------------------------------
    def _release_rate(self, inst: int | None, tick: int) -> float | None:
        """How the instrument's release reaches the MOD at `tick`: None for a cut (C00 / ECx, as
        the hardware's instant release or a sample that has nothing to release), else its rate
        in dB per second for _write_release."""
        if not self._ctx.release_slides or inst is None:
            return None
        member = self._bank_member
        if member is not None and member.inst == inst:
            rate = self._ctx.release.get(member.primary)   # its own sound's, not the bank slot's
        else:
            rate = self._ctx.release.get(inst)
        if rate is None or rate == math.inf:
            return None
        row_secs = self._timeline.row_secs(tick)
        if self._config.target_speed < 2 or (rate > 0 and 30.0 / rate < row_secs):
            return None                 # over within a row: a cut is closer than a slide
        return rate

    def _write_release(self, mod_chan: int, row_total: int, volume: int, rate_db_s: float,
                       tick: int, stop_row_total: int) -> set[tuple[int, int]]:
        """End a note the way the chip does: a volume slide from `volume` at the voice's release
        rate, one `A0y` per row from `row_total` on, stopping before `stop_row_total` (the next
        note-on's row) or once the volume is gone.

        The YM2612 release is linear in dB, so the target volume falls by the same ratio every
        row; each row's y is what takes the volume from where the last row left it to where
        the curve is at the row's end (rows whose share rounds to nothing are skipped, so a slow
        release keeps its pace).  A row whose effect slot is taken is skipped.  If the volume
        is still up after _MAX_RELEASE_ROWS (a release rate of 0, which rings on the hardware),
        a C00 ends it.  Returns the (pattern, row) cells written.
        """
        speed = self._config.target_speed
        row_secs = self._timeline.row_secs(tick)
        per_tick = speed - 1
        written: set[tuple[int, int]] = set()
        v = float(volume)
        target = float(volume)
        for r in range(row_total, min(stop_row_total, self._config.max_patterns * 64)):
            if v <= 0:
                break
            pattern, row = divmod(r, 64)
            if r - row_total >= _MAX_RELEASE_ROWS:
                self._mod.set_cursor(pattern, mod_chan, row)
                if self._mod.effect_slot_free(pattern, row, mod_chan):
                    self._mod.set_effect(0xC, 0)
                    written.add((pattern, row))
                break

            # This row's share of the fall
            target *= db_to_gain(-rate_db_s * row_secs)
            y = min(15, round((v - target) / per_tick))
            if y <= 0:
                continue
            self._mod.ensure_pattern(pattern)
            if not self._mod.effect_slot_free(pattern, row, mod_chan):
                continue
            self._mod.set_cursor(pattern, mod_chan, row)
            self._mod.set_effect(0xA, y)
            written.add((pattern, row))
            v = max(0.0, v - y * per_tick)
        return written

    def _cut_after(self, mod_chan: int, tick: int, secs: float, next_row: int) -> bool:
        """Cut the note that started at `tick` `secs` later: `C00` on the row the cut falls
        on, `ECx` inside it, unless the channel's next note-on (`next_row`, from
        _next_note_row) is there first.  Seconds are V-int frames at the region's frame rate,
        then driver ticks as a note fill is (ticks_per_frame_at), so a tempo change is honoured.
        Leaves the cursor on the cut's cell; returns whether one was written."""
        fps = self._config.fps
        speed = self._config.target_speed
        tpr = self._timeline.ticks_per_row
        cut_ticks = secs * fps * self._timeline.ticks_per_frame_at(tick)
        cut_abs = max(round(tick * speed / tpr) + 1, round((tick + cut_ticks) * speed / tpr))
        row_total, sub = divmod(cut_abs, speed)
        if row_total >= next_row or row_total // 64 >= self._config.max_patterns:
            return False
        self._write_cut(mod_chan, row_total, sub)
        return True

    def _write_cut(self, mod_chan: int, row_total: int, sub: int) -> None:
        """C00 on the row (over the whole song), or ECx `sub` MOD ticks into it.  Leaves the
        cursor there."""
        self._mod.set_cursor(row_total // 64, mod_chan, row_total % 64)
        if sub:
            self._mod.set_effect(0xE, 0xC0 | sub)
        else:
            self._mod.set_effect(0xC, 0)

    def _clear_stale_cut(self, pattern: int, row: int, mod_chan: int) -> None:
        """Drop a C00 left in this cell by an earlier rest whose row rounds onto this note-on's:
        set_note keeps the effect bytes, and a note-on with C00 is a silent note."""
        if not self._mod.note_at(pattern, row, mod_chan) and self._mod.effect_at(pattern, row, mod_chan) == (0xC, 0):
            self._mod.set_cursor(pattern, mod_chan, row)
            self._mod.set_effect(0, 0)

    # --- ties -----------------------------------------------------------------------------------
    def _retune_tie(self, tick: int) -> None:
        """A tie sounding at a detune the note-on did not: E1x / E2x on the tie's row."""
        assert self._sounding_cents is not None and self._last_chip is not None
        pattern, row = self._timeline.pattern_row(tick)
        mod_chan = self._router.current(tick)
        if pattern >= self._config.max_patterns:
            return
        if (pattern, row) == self._last_note_cell or self._router.borrowed(mod_chan, tick):
            return                      # the attack row's slide would retune the attack too
        self._mod.ensure_pattern(pattern)
        want = detune_cents(self._last_chip, self._st.detune, self._ctx.song.fm_frequencies) - self._sounding_cents
        moved = self._fine_slide(pattern, row, mod_chan, self._sounding_period, want)
        if moved is not None:
            self._sounding_period -= moved
            self._sounding_cents += 1200.0 * math.log2((self._sounding_period + moved) / self._sounding_period)

    def _fine_slide(self, pattern: int, row: int, mod_chan: int, period: int, cents: float) -> int | None:
        """E1x / E2x moving a sounding note `cents` (up: positive) from `period`, on a cell with
        no note and a free effect slot → the period units it moved (up: positive), else None."""
        units = round(period - period * 2.0 ** (-cents / 1200.0))
        units = max(-_FINE_SLIDE_MAX, min(_FINE_SLIDE_MAX, units))
        if not units:
            return None
        retunes = self._ctx.stats.tie_retunes
        if self._mod.note_at(pattern, row, mod_chan) or not self._mod.effect_slot_free(pattern, row, mod_chan):
            retunes['skipped'] += 1
            return None
        self._mod.set_cursor(pattern, mod_chan, row)
        self._mod.set_effect(0xE, (0x10 if units > 0 else 0x20) | abs(units))
        retunes['placed'] += 1
        return units

    # --- warnings -------------------------------------------------------------------------------
    def _warn_resolution(self, res: ResolvedNote, note) -> None:
        """Warn where a note was clamped to the MOD's three octaves, or fell outside every range
        of a mapped voice (a config gap rather than an intended fallback)."""
        st, source = self._st, self._cfg.source
        src_name = _semitone_to_name(res.source)
        if res.path == "transpose" and st.fm_ranges(source) and st.voice is not None:
            ranges = st.fm_ranges(source)
            self._ctx.diag.warn(WarningKind.MAP_GAP, channel=source, voice_idx=st.voice, extra_ctx=st.psg_label,
                                note_name=src_name, semitone=res.source, range_lo=_semitone_to_name(ranges[0].low),
                                range_hi=_semitone_to_name(ranges[-1].high))
        if not res.clamped:
            return
        high = res.raw_index > _HIGHEST_NOTE
        entry = res.entry
        kind = WarningKind.CLAMP_HIGH if high else WarningKind.CLAMP_LOW
        w = {'channel': source, 'src_name': src_name, 'note_value': note.note_value, 'transpose': 0}
        if res.path == "fm_root":
            assert entry is not None
            w.update(voice_idx=st.voice,
                     boundary=_semitone_to_name(entry.high if high else entry.low))
        elif res.path == "psg_root":
            assert entry is not None
            # The source note at which the anchor runs off the MOD's range
            edge = entry.low + ((_HIGHEST_NOTE if high else 0) - entry.root.value)
            w.update(voice_idx=None, extra_ctx=st.psg_label, boundary=_semitone_to_name(edge))
        else:
            tr = res.total_transpose
            w.update(voice_idx=st.voice, extra_ctx=st.psg_label, transpose=tr,
                     boundary=_semitone_to_name((_HIGHEST_NOTE if high else 0) - tr))
            if source.startswith('PSG') and not st.psg_label and self._config.psg_voice_map:
                w['psg_available_labels'] = list(self._config.psg_voice_map.keys())
        self._ctx.diag.warn(kind, **w)
