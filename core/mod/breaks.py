"""Pattern breaks (`mod_pattern_breaks`): a pattern ended early, the rows after it repacked.

A break at (pattern P, row R) cuts P short with a Bxx to P+1 and moves every later row up so
that nothing is skipped:

    before   P: [0 .. R | R+1 .. 63]     P+1: [0 .. 63]                       ...
    after    P: [0 .. R + Bxx | empty]   P+1: [P's R+1 .. 63, P+1's 0 .. R]   ...  (+1 pattern at the end)

A Bxx aimed at a later pattern follows its target row; one landing mid-pattern gets a Dxx.
"""

from .cell import ROWS_PER_PATTERN
from .file import ModFile


def row_to_bcd(row: int) -> int:
    """BCD-encode a row number for Dxx (pattern break): 32 -> 0x32, 10 -> 0x10."""
    return ((row // 10) << 4) | (row % 10)


def shift_for_breaks(flat_row: int, breaks: list[tuple[int, int]] | None) -> int:
    """Move a pre-break flat row index to where apply_pattern_breaks put it.

    Each break at (pattern P, row R) pushes everything from flat row P*64+R+1 onward
    to the start of pattern P+1, i.e. forward by the 63-R rows it blanked out.
    """
    for P, break_row in sorted(breaks or []):
        if flat_row >= P * ROWS_PER_PATTERN + break_row + 1:
            flat_row += ROWS_PER_PATTERN - 1 - break_row
    return flat_row


def phrase_breaks(first: int, rows: int, total: int) -> list[tuple[int, int]]:
    """The breaks that cut `total` rows into patterns of `rows`, the first `first` rows (a pickup)
    a pattern of their own; at 64 rows, the pickup's alone.  Each in the rows the ones before it
    left, as apply_pattern_breaks takes them.  first 32, rows 40: (0, 31), (1, 39), (2, 39) ..."""
    breaks = [(0, first - 1)] if first else []
    if rows >= ROWS_PER_PATTERN:
        return breaks

    pattern, row = len(breaks), first
    while row + rows < total:
        breaks.append((pattern, rows - 1))
        pattern, row = pattern + 1, row + rows
    return breaks


def apply_pattern_breaks(mod: ModFile, breaks: list) -> None:
    """Each (pattern, row) of `breaks`, ascending: the rows after it repacked from the next
    pattern's row 0, the Bxx aimed past it retargeted, a Bxx to the next pattern at the break.

    Raises ValueError if the extra pattern the repacked rows need would pass MAX_POSITIONS.
    """
    for pattern, row in sorted(breaks):
        body = _body(mod, pattern, row)
        _repack(mod, pattern, row, body)
        _retarget_jumps(mod, pattern, pattern * ROWS_PER_PATTERN + row + 1)

        # The break itself: Bxx to the next pattern on the first free effect slot
        channel = mod.free_effect_channel(pattern, row) or 0
        mod.put_effect(pattern, row, channel, 0xB, pattern + 1)


def _body(mod: ModFile, pattern: int, row: int) -> list[bytes]:
    """Every row after the break, in play order: the break pattern's rest, then each later pattern."""
    rows = [mod.patterns[pattern].row_bytes(r) for r in range(row + 1, ROWS_PER_PATTERN)]
    for later in mod.patterns[pattern + 1:]:
        rows += [later.row_bytes(r) for r in range(ROWS_PER_PATTERN)]
    return rows


def _repack(mod: ModFile, pattern: int, row: int, body: list[bytes]) -> None:
    """The break pattern emptied after `row`; `body` packed from the next pattern's row 0, a
    pattern appended where it runs past the last (GHZ: 1056 rows into 17 patterns)."""
    needed = -(-len(body) // ROWS_PER_PATTERN)
    extra = max(0, needed - (len(mod.patterns) - 1 - pattern))
    if mod.positions + extra > mod.MAX_POSITIONS:
        raise ValueError(f"apply_pattern_breaks: {extra} extra pattern(s) needed but "
                         f"would exceed MAX_POSITIONS ({mod.MAX_POSITIONS})")
    mod.add_patterns(extra)

    for r in range(row + 1, ROWS_PER_PATTERN):
        mod.patterns[pattern].clear_row(r)
    for i in range(needed):
        target = mod.patterns[pattern + 1 + i]
        for r in range(ROWS_PER_PATTERN):
            moved = i * ROWS_PER_PATTERN + r
            if moved < len(body):
                target.put_row(r, body[moved])
            else:
                target.clear_row(r)


def _retarget_jumps(mod: ModFile, pattern: int, body_start: int) -> None:
    """Every Bxx aimed past the break pattern sent where its target row now is.

    Its target T (row 0) was flat row T*64; the body moved it to P+1 + (T*64 - body_start) // 64,
    row (T*64 - body_start) % 64.  A row other than 0 takes a Dxx (BCD) beside the Bxx, on
    another free effect slot of the same row."""
    for p in range(len(mod.patterns)):
        for r in range(ROWS_PER_PATTERN):
            for ch in range(mod.CHANNELS):
                effect, target = mod.effect_at(p, r, ch)
                if effect != 0xB or target <= pattern or target * ROWS_PER_PATTERN < body_start:
                    continue
                new_pattern, new_row = divmod(target * ROWS_PER_PATTERN - body_start, ROWS_PER_PATTERN)
                mod.put_effect(p, r, ch, 0xB, (pattern + 1 + new_pattern) & 0x7F)
                if not new_row:
                    continue
                others = [c for c in range(mod.CHANNELS) if c != ch]
                dxx = mod.free_effect_channel(p, r, others)
                if dxx is not None:
                    mod.put_effect(p, r, dxx, 0xD, row_to_bcd(new_row))
