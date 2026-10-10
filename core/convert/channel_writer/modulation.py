"""smpsModSet / smpsModOn / smpsModOff on one channel, and what each note carries of it: 4xy on
its rows, or - a modulation cycling too slowly for 4xy - a slide per row (core.convert.vibrato
has the arithmetic)."""

import math

from ...mod import PERIOD_TABLE
from ...plan import DriverState, detune_cents
from ...smps import ChannelType, CoordFlag, ModSet, SmpsChannel, TrackRules
from ..vibrato import modulation_slides, vibrato_depth
from .cells import Cells
from .context import WriterContext
from .note import FillCut, NoteOn


class Modulation:
    """One channel's modulation, as the coordination flags set it."""

    def __init__(self, ctx: WriterContext, cells: Cells, channel: SmpsChannel, source: str, track: TrackRules):
        self._ctx, self._cells, self._timeline, self._config = ctx, cells, ctx.timeline, ctx.config
        self._rules, self._source, self._track = channel.rules, source, track
        self._is_psg = channel.header.channel_type == ChannelType.PSG

        self._active = False
        self._speed = 0             # 4xy speed
        self._change = 0            # raw SMPS delta byte (FNUM / PSG divider units); scaled per note
        self._steps = 0             # raw SMPS steps byte
        self._wait = 0              # frames to delay before vibrato starts
        self._mod_set: ModSet | None = None
        self._slides = False        # cycles too slowly for 4xy: slides (VibratoSpeed.too_slow)
        self._origin = 0            # where the modulation started: the last attack, or a ModSet / ModOn
                                    # since (Streets of Rage $8B FM2: a ModSet on a tie starts it there)

    @property
    def active(self) -> bool:
        return self._active

    def apply(self, eff, tick: int) -> None:
        """A coordination flag: smpsModSet, smpsModOn, smpsModOff change the modulation; any
        other leaves it."""
        if isinstance(eff, ModSet):
            self._wait = eff.wait
            self._change = eff.delta       # raw delta; scaled to period units at placement
            self._steps = eff.steps
            self._speed = self._ctx.vibrato.speed(eff.speed, eff.steps, self._source, tick, self._track)
            self._mod_set = eff
            self._slides = self._ctx.vibrato.too_slow(eff.speed, eff.steps, tick, self._track)
            self._origin = tick
            self._active = True
        elif eff.flag == CoordFlag.MOD_ON:
            self._origin = tick
            self._active = True
        elif eff.flag == CoordFlag.MOD_OFF:
            self._active = False

    def write(self, n: NoteOn, st: DriverState, entry: object | None, col: int,
              attack_free: bool, fill: FillCut, cxx_cell: tuple[int, int] | None) -> None:
        """What the note carries of the modulation: 4xy on the attack row (`attack_free`: its
        slot is free) and on the rows after it, or slides.  `entry`: the voice_map range the
        note matched, whose `vibrato` overrides the smpsModSet's; `fill` and `cxx_cell`: cells
        of the note's rows already taken."""
        speed, depth = (0, 0) if self._slides else self._vibrato_of(n, st, entry)

        # On the attack row when the modulation wait is over for most of the row
        wait_ticks = self._wait * self._timeline.ticks_per_frame_at(n.tick)
        if attack_free and n.vib_on and speed > 0 and wait_ticks <= self._timeline.ticks_per_row / 2:
            self._cells.put(n.pattern, n.row, col, 0x4, (speed << 4) | depth)

        if n.vib_on and speed > 0:
            self._continue(n, col, speed, depth, fill, cxx_cell)
        if not n.event.note.is_no_attack:
            self._origin = n.tick
        if n.vib_on and self._slides and not n.legato:
            self._slide(n, st, col)

    def _vibrato_of(self, n: NoteOn, st: DriverState, entry: object | None) -> tuple[int, int]:
        """(4xy speed, depth) for this note: a per-entry override first, else the smpsModSet's
        speed and the depth of its swing at this note's frequency word."""
        override = getattr(entry, "vibrato", None)
        if override is None and st.psg_entry is not None:
            override = st.psg_entry.vibrato
        if override is not None:
            return (override >> 4) & 0xF, override & 0xF

        # Depth is per note: the driver's swing is a fixed number of FNUM / divider units, so its
        # size in cents depends on the chip note it is added to.
        depth = vibrato_depth(self._change, self._steps, PERIOD_TABLE[n.mod_note.value],
                              n.res.source + st.transpose, self._is_psg, st.psg_read,
                              self._rules.fm_frequencies, self._ctx.player, self._track)
        return (self._speed if depth else 0), depth

    def _continue(self, n: NoteOn, col: int, speed: int, depth: int, fill: FillCut,
                  cxx_cell: tuple[int, int] | None) -> None:
        """4xy on every continuation row within the note's vibrato span.  In ProTracker, 4xy only
        applies on rows where the effect is present, so it is repeated each row to get continuous
        vibrato matching SMPS modulation.  The SMPS wait is in FRAMES (DoModulation runs on
        TempoWait frames too); a row carries 4xy when modulation runs for at least half of it."""
        vib_start_tick = n.tick + self._wait * self._timeline.ticks_per_frame_at(n.tick)
        tpr = self._timeline.ticks_per_row
        fill_cell = fill.cell if fill.placed else None

        # The rows of the grid after the one the note went on, not whole rows counted from its
        # tick: a note that starts between rows (EDx) would otherwise reach a row past its end,
        # the next note's attack row (Sonic 1's Robotnik theme at 3 ticks per row: a $04
        # triplet's 4xy started the vibrato of the long note after it 200 ms early).  A song's
        # last note rings into patterns nothing has written yet: Sonic 1's Game Over closing G#3
        # (3 s of smpsModSet) had no 4xy at all while the check here stopped at them
        for tick in self._cells.grid_ticks(n.row_total + 1, n.tick + n.duration):
            if tick + tpr / 2 < vib_start_tick:
                continue                    # still waiting
            cell = self._timeline.pattern_row(tick)
            if not self._cells.in_song(cell[0]):
                break
            if fill.slides and cell >= min(fill.slides):
                break                       # released: nothing to modulate
            if cell in (fill_cell, cxx_cell):
                continue
            self._cells.put(*cell, col, 0x4, (speed << 4) | depth)

    def _slide(self, n: NoteOn, st: DriverState, col: int) -> None:
        """A modulation too slow for 4xy as slides: each row with a free effect slot slides to the
        chip's pitch at its end, until the note ends or is cut (Streets of Rage's sweeps).  A tie
        written as a note (a level change) starts from the note's period again; its modulation
        runs on from the attack."""
        assert self._mod_set is not None
        tpr = self._timeline.ticks_per_row
        tpf = self._timeline.ticks_per_frame_at(n.tick)
        end = n.tick + n.duration if n.cut_tick is None else min(n.tick + n.duration, n.cut_tick)

        # The rows from the attack's on, those whose effect slot is free
        rows = []
        for start in self._cells.grid_ticks(n.row_total, end):
            pattern, row = self._timeline.pattern_row(start)
            if not self._cells.in_song(pattern):
                break
            if self._cells.free(pattern, row, col):
                rows.append((start, min(start + tpr, end)))

        slides = modulation_slides(self._mod_set, PERIOD_TABLE[n.mod_note.value], rows,
                                   lambda tick: max(0.0, (tick - self._origin) / tpf), self._cents(n, st),
                                   self._config.target_speed - 1, self._track)
        for start, effect, param in slides:
            pattern, row = self._timeline.pattern_row(start)
            self._cells.put(pattern, row, col, effect, param)

    def _cents(self, n: NoteOn, st: DriverState):
        """The pitch a modulation offset moves `n` by: added to its FNUM word, or its PSG divider."""
        if not self._is_psg:
            return lambda offset: detune_cents(n.res.chip, offset, self._rules.fm_frequencies)
        divider = st.psg_read[(n.res.source + st.transpose) & 0x7F]
        return lambda offset: 1200 * math.log2(divider / max(1, divider + offset)) if divider > 0 else 0.0
