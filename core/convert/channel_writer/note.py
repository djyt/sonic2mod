"""One melodic note-on while it is written, and where its fill cut went."""

from dataclasses import dataclass, field

from ...mod import ModNote
from ...plan import ResolvedNote
from .cells import Cells


@dataclass
class NoteOn:
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
    volume: int = 0                     # its MOD volume (once legato chose its instrument)
    needs_cxx: bool = False             # ... which differs from its instrument's
    pattern: int = 0
    row: int = 0
    delay: int = 0                      # EDx, MOD ticks

    @property
    def cell(self) -> tuple[int, int]:
        return self.pattern, self.row

    @property
    def row_total(self) -> int:
        return Cells.row_total(self.pattern, self.row)


@dataclass
class FillCut:
    """Where a note's fill cut went (ChannelWriter._place_fill)."""
    placed: bool = False
    slot_used: bool = False             # the cut (ECx) or the slide's first A0y took the attack row
    pattern: int = -1
    row: int = -1
    slides: set = field(default_factory=set)    # rows a release slide took

    @property
    def cell(self) -> tuple[int, int]:
        return self.pattern, self.row
