#!/usr/bin/env python3
"""Playback-semantics lint for a MOD: the notes a ProTracker player cannot sound.

The regression suite compares pattern cells, which says whether a conversion changed, not
whether what it wrote can be heard.  This walks each channel of a MOD the way a ProTracker
player does — which sample is playing, whether it has data left, what the volume is — and
reports every cell that is written as a sound but plays silence:

    portamento_silent   a `3xx` (tone portamento: no re-trigger) on a channel whose sample has
                        played out or was never started — the note never sounds
    portamento_muted    a `3xx` on a channel at volume 0 with no instrument number to reset it
    no_sample           a note-on whose instrument has no sample data (an empty slot)
    empty_instrument    an instrument number on a cell whose slot is empty
    note_muted          a note-on written with `C00` in its own cell: it triggers at volume 0

A one-shot sample is silent once its data has played (its length in seconds at the note's
period, PAL Amiga clock); a looped one plays until stopped.  These are ProTracker's rules and
FT2 clone's (the player the MODs are checked in): an instrument number on a `3xx` row never
swaps the playing sample, and a `3xx` on an idle channel plays nothing.  OpenMPT/libopenmpt
differs on both (it swaps the sample and starts an idle channel), which is why the VGZ audit,
rendered through libopenmpt, does not see these.  Speed and tempo follow the
`Fxx` commands; `Bxx`/`Dxx` end the pass (one pass through the song is walked, plus the
loop's first pattern once more with the end-of-song state, which is what a player does).

    python tools/mod_lint.py output/02_green_hill_zone.mod
    python tools/mod_lint.py a.mod b.mod        # every file; exit 1 if any issue

As a library: `lint_mod(path) -> list[dict]` (each with 'type', 'pattern', 'row', 'channel',
'instrument', 'detail'), used by tools/regression_test.py: a case fails when the new MOD has
an issue its baseline does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from tools.mod_compare import parse_mod

AMIGA_CLOCK = 3_546_895


def _sample_headers(path: str) -> list[dict]:
    """[{length, loop_start, loop_len} in bytes] for the 31 slots, from the raw header."""
    data = Path(path).read_bytes()
    out = []
    off = 20
    for _ in range(31):
        length = int.from_bytes(data[off + 22:off + 24], "big") * 2
        loop_start = int.from_bytes(data[off + 26:off + 28], "big") * 2
        loop_len = int.from_bytes(data[off + 28:off + 30], "big") * 2
        out.append({"length": length, "loop_start": loop_start, "loop_len": loop_len})
        off += 30
    return out


def lint_mod(path: str) -> list[dict]:
    mod = parse_mod(path)
    headers = _sample_headers(path)
    pats = mod["patterns"]
    nch = mod["num_channels"]
    order = mod["position_list"][:mod["song_length"]]

    def sample_secs(inst: int, period: int) -> float | None:
        """Seconds the sample of `inst` plays at `period`; None = loops (plays on)."""
        h = headers[inst - 1]
        if h["loop_len"] > 2:
            return None
        return h["length"] / (AMIGA_CLOCK / period) if period else 0.0

    issues: list[dict] = []
    speed, bpm = 6, 125
    # Per-channel player state
    inst = [0] * nch            # instrument whose sample is playing (0 = none)
    remaining = [0.0] * nch     # seconds of one-shot sample left (inf = looping)
    volume = [0] * nch
    period = [0] * nch

    def walk_position(pos_index: int, stop_at_jump: bool) -> tuple[int, int] | None:
        """Play one position; returns (target position, row) of a Bxx/Dxx, or None."""
        nonlocal speed, bpm
        pat = pats[order[pos_index]]
        for r in range(64):
            row = pat[r]
            jump = None
            # Fxx first: a speed on this row applies to this row
            for ch in range(nch):
                per, ins, eff, par = row[ch]
                if eff == 0xF and par:
                    if par < 32:
                        speed = par
                    else:
                        bpm = par
            row_secs = speed * 2.5 / bpm
            for ch in range(nch):
                per, ins, eff, par = row[ch]
                where = {"pattern": order[pos_index], "row": r, "channel": ch, "instrument": ins}
                if ins and headers[ins - 1]["length"] < 4:
                    issues.append({"type": "empty_instrument" if not per else "no_sample",
                                   "detail": f"instrument {ins} has no sample", **where})
                if per and eff == 0x3:
                    # Tone portamento: no re-trigger.  Something must already be playing.
                    if inst[ch] == 0 or remaining[ch] <= 0:
                        issues.append({"type": "portamento_silent", **where,
                                       "detail": ("no sample playing" if inst[ch] == 0
                                                  else f"instrument {inst[ch]}'s sample has played out")})
                    elif volume[ch] == 0 and not ins:
                        issues.append({"type": "portamento_muted", **where,
                                       "detail": f"channel at volume 0 (instrument {inst[ch]})"})
                    if ins:
                        volume[ch] = 64      # reset to the sample's volume (a value we do not track)
                    period[ch] = per
                elif per:
                    if eff == 0xC and par == 0:
                        issues.append({"type": "note_muted", **where, "detail": "note-on with C00 in its cell"})
                    if ins:
                        inst[ch] = ins
                        volume[ch] = 64
                    period[ch] = per
                    if inst[ch]:
                        secs = sample_secs(inst[ch], per)
                        remaining[ch] = float("inf") if secs is None else secs
                elif ins:
                    volume[ch] = 64
                if eff == 0xC:
                    volume[ch] = par
                elif eff == 0xE and (par >> 4) == 0xC:
                    volume[ch] = 0
                elif eff == 0xB:
                    jump = (par, 0)
                elif eff == 0xD:
                    # after a Bxx in the row, its position (ProTracker reads the row left to right)
                    jump = (jump[0] if jump is not None else pos_index + 1, (par >> 4) * 10 + (par & 0xF))
            for ch in range(nch):
                if remaining[ch] != float("inf"):
                    remaining[ch] = max(0.0, remaining[ch] - row_secs)
            if jump is not None:
                return jump
        return None

    pos = 0
    seen: set[int] = set()
    loop_to = None
    while pos < len(order):
        seen.add(pos)
        target = walk_position(pos, True)
        if target is None:
            pos += 1
            continue
        if target[0] in seen or target[0] >= len(order):
            loop_to = target[0]                  # a backward jump: the song loops here
            break
        pos = target[0]                          # a forward jump (a pattern break): follow it
    if loop_to is not None and loop_to < len(order):
        # The loop's first position once more, with the state the song's end leaves
        walk_position(loop_to, False)
    return issues


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    total = 0
    for path in sys.argv[1:]:
        issues = lint_mod(path)
        total += len(issues)
        print(f"{path}: {len(issues)} issue(s)")
        for i in issues[:40]:
            print(f"  {i['type']:18} pattern {i['pattern']:2d} row {i['row']:2d} ch {i['channel'] + 1}: {i['detail']}")
        if len(issues) > 40:
            print(f"  ... and {len(issues) - 40} more")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
