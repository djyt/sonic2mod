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

    writer.py         ChannelWriter: the walk; rests, drum hits, melodic notes
    router.py         ColumnRouter: the column a note goes to in the merged build
    levels.py         Levels: the MOD volume a note plays at
    modulation.py     Modulation: smpsModSet -> 4xy rows, or slides
    fades.py          Fades: release slides, a sliding loop's fall
    note_warnings.py  warn_resolution: clamped notes, voice_map gaps
    note.py           NoteOn, FillCut: one melodic note while it is written
    cells.py          Cells: the song's rows - commands, cuts, EDx placement, fine slides
    context.py        WriterContext, EmissionStats: what every channel's writer shares

    writer ─► router | levels | modulation | fades | note_warnings ─► note ─► cells ─► context
"""

from .context import EmissionStats, WriterContext
from .writer import ChannelWriter

__all__ = ["ChannelWriter", "EmissionStats", "WriterContext"]
