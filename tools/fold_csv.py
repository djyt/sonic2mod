#!/usr/bin/env python3
"""Turn a per-pattern fold table into a config's `merge_patterns:` section.

The table is a CSV with one row per pattern of the reference MOD and one column per MOD
channel, saying which channels share a channel in that pattern (Green Hill's is
`input/02_ghz_fold.csv`):

    Pattern,Ch 1,Ch 2,Ch 3,Ch 4,Ch 5,Ch 6,Ch 7,Ch 8,Ch 9
    ,DAC Drums,Lead,Bass,,,,,,Hi-Hat                  <- an optional row of names (blank pattern cell)
    0,fold 1,keep,keep,keep,,,,,fold 1
    1,fold 1,keep,fold 1,fold 2,fold 2,fold 2,fold 2,keep,fold 1
    ...
    10,fold 1,keep,keep,fold 2,fold 2,keep,fold 3,fold 3,fold 1

Pattern numbers are hex, as Fast Tracker shows them.  `Ch N` is MOD channel N of the
reference build (the config's `mod_channel: N-1`); a column may name the source instead
("FM1").  A cell is `fold N` (the channels with the same N share one channel in that pattern),
`keep` (its own channel), `drop` (its notes are left out there), or blank: kept when the
channel plays anything in that pattern, dropped (silently) when it does not.  `fold N*` names
that channel the fold's primary (the ear's choice over the measurement below).

A fold is written as a merge group, and a group needs a **primary** (the channel the others
fold onto, whose effects apply).  The table cannot say which, so this tool measures: every
member is tried as the primary over the fold's patterns, the way `tools/merge_survey.py`
measures a pair, and the one that loses the fewest follower notes wins (ties: the most
folded, then the fewest composite instruments).  The per-candidate counts are printed so the
choice can be overruled by hand in the YAML.  A drum primary gets `cut_primary: true` (a
follower's note over a drum's decay cuts it, as on any 4-channel Amiga).

Rows with the same cells are joined into one block ("1-4", "d-10").  `--bank` marks every drum-primary group `bank: true`, so
its mixes share instrument slots as sample banks (core/banks.py).  The output is the YAML
to paste into the config; `--write` puts it there, between marker comments, replacing the
block it wrote last time.  The config is the source of truth: the table is the quick way to
draft the folds, and the block can be edited by hand afterwards (a re-run with `--write`
replaces it from the table again).

    python tools/fold_csv.py configs/02_green_hill_zone.yaml input/02_ghz_fold.csv
    python tools/fold_csv.py configs/02_green_hill_zone.yaml input/02_ghz_fold.csv --write
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig, format_patterns
from core.merge import PairStats
from tools.merge_survey import SurveyContext, survey_context

MARK_BEGIN = "# >>> merge_patterns - written by tools/fold_csv.py from {csv}; the config is the source of truth: edit it here, or re-run --write to replace the block from the table"
MARK_END = "# <<< merge_patterns"


class Cell:
    KEEP, DROP, BLANK = "keep", "drop", ""

    @staticmethod
    def parse(text: str) -> str | tuple[int, bool]:
        """'fold 2' -> (2, False); 'fold 2*' -> (2, True): the fold's primary; 'keep' / 'drop' /
        '' as they are."""
        t = text.strip().lower()
        if t in ("", Cell.KEEP, Cell.DROP):
            return t
        m = re.fullmatch(r"(?:fold|merge|group)\s*(\d+)\s*(\*?)", t)
        if m:
            return int(m.group(1)), bool(m.group(2))
        raise ValueError(f"cell {text!r}: expected 'fold N', 'fold N*', 'keep', 'drop' or blank")


def read_table(path: str, cfg: ConversionConfig) -> tuple[list[str], dict[int, list], dict[str, str]]:
    """(sources per column, {pattern: cells}, {source: name from the names row})."""
    by_mod = {c.mod_channel: c.source for c in cfg.channels}
    sources_all = {c.source for c in cfg.channels}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError(f"{path}: empty")
    header = rows[0]
    columns: list[str] = []
    for raw in header[1:]:
        h = raw.strip()
        m = re.fullmatch(r"(?:ch(?:annel)?\.?\s*)(\d+)", h, re.IGNORECASE)
        if m:
            src = by_mod.get(int(m.group(1)) - 1)
            if src is None:
                raise ValueError(f"{path}: column {h!r} is MOD channel {m.group(1)}, which no channels: entry has")
        elif h.upper() in sources_all:
            src = h.upper()
        else:
            raise ValueError(f"{path}: column {h!r} is neither 'Ch N' nor a channel source (FM1, PSG3 ...)")
        columns.append(src)
    table: dict[int, list] = {}
    names: dict[str, str] = {}
    for row in rows[1:]:
        r = row + [""] * (len(columns) + 1 - len(row))
        if not r[0].strip():                  # the names row
            for src, name in zip(columns, r[1:len(columns) + 1], strict=True):
                if name.strip():
                    names[src] = name.strip()
            continue
        try:
            pattern = int(r[0].strip(), 16)
        except ValueError:
            raise ValueError(f"{path}: pattern {r[0]!r} is not a hex number") from None
        if pattern in table:
            raise ValueError(f"{path}: pattern {pattern:x} appears twice")
        table[pattern] = [Cell.parse(c) for c in r[1:len(columns) + 1]]
    return columns, table, names


def runs(table: dict[int, list]) -> list[tuple[list[int], list]]:
    """Consecutive patterns with the same cells, in order: [([1, 2, 3, 4], cells), ...]."""
    out: list[tuple[list[int], list]] = []
    for p in sorted(table):
        if out and out[-1][1] == table[p] and out[-1][0][-1] == p - 1:
            out[-1][0].append(p)
        else:
            out.append(([p], table[p]))
    return out


def kind_of(source: str) -> str:
    return "DAC" if source == "DAC" else source[:2] if source.startswith("FM") else "PSG"


def choose_primary(ctx: SurveyContext, members: list[str], patterns: frozenset, named: str | None = None
                   ) -> tuple[str, list[tuple[str, list[PairStats]]]]:
    """Try every member as the primary of the fold over `patterns`; the drums own a fold they
    are in (every other note cuts a drum's decay, as on any Amiga cover, and the fold then
    lands on the drum column in every pattern), otherwise the winner plays the fewest follower
    notes wrong: lost ones, and paired ones the primary's own effects would distort — a fill
    shorter than the follower's note cuts the whole composite (a chime's 16-frame fill over a
    16-tick chord), and the primary's vibrato is the composite's (a chime's wobble on a chord
    that has none).  A primary `named` in the table (`fold N*`) is taken as it is; the counts
    are measured all the same.  Returns (primary, [(candidate, its follower stats)] in the order
    tried)."""
    tried: list[tuple[str, list[PairStats]]] = []
    for p in members:
        cut = kind_of(p) == "DAC"
        stats = [ctx.pair(p, f, patterns, cut_primary=cut) for f in members if f != p]
        tried.append((p, stats))

    def score(item):
        p, stats = item
        keys = set()
        for s in stats:
            keys |= s.keys
        wrong = sum(s.lost for s in stats) + distorted(ctx, p, [s.follower for s in stats], patterns)
        return (kind_of(p) != "DAC", wrong, -sum(s.paired + s.solo for s in stats), len(keys))
    return (named if named is not None else min(tried, key=score)[0]), tried


def distorted(ctx: SurveyContext, primary: str, followers: list[str], patterns: frozenset) -> int:
    """Follower notes a composite on `primary` would play wrong: those its fill would cut short
    (the primary's fill applies to the whole composite; a follower's own fill is honoured
    inside it) and those whose modulation differs from the primary's."""
    p_notes, _ = ctx.restrict(primary, patterns)
    n = 0
    for f in followers:
        f_notes, _ = ctx.restrict(f, patterns)
        for t, fn in f_notes.items():
            p = p_notes.get(t)
            if p is None:
                continue
            if p.fill_secs is not None:
                secs = ctx.conv.tick_span_secs(t, t + min(fn.duration, fn.sounding))
                own = fn.fill_secs if fn.fill_secs is not None else secs
                if p.fill_secs < min(secs, own):
                    n += 1
            if p.vibrato != fn.vibrato:
                n += 1
    return n


def build(ctx: SurveyContext, columns: list[str], table: dict[int, list],
          bank: bool = False, mix_note: str | None = None) -> tuple[list[dict], list[str]]:
    """The blocks of the merge_patterns section and the report lines that explain them."""
    counts_in = {src: {} for src in columns}          # source -> {pattern: notes}
    for src in columns:
        if src not in ctx.notes:
            continue
        for t in ctx.notes[src][0]:
            p = ctx.pattern_of(t)
            counts_in[src][p] = counts_in[src].get(p, 0) + 1
    blocks: list[dict] = []
    report: list[str] = []
    for pats, cells in runs(table):
        patterns = frozenset(pats)
        where = format_patterns(patterns)
        folds: dict[int, list[str]] = {}
        named: dict[int, str] = {}
        drop: list[str] = []
        kept: list[str] = []
        for src, cell in zip(columns, cells, strict=True):
            n = sum(v for p, v in counts_in[src].items() if p in patterns)
            if isinstance(cell, tuple):
                fid, lead = cell
                folds.setdefault(fid, []).append(src)
                if lead:
                    if fid in named:
                        raise ValueError(f"pattern {where}: fold {fid} names two primaries ({named[fid]} and {src})")
                    named[fid] = src
            elif cell == Cell.DROP:
                drop.append(src)
                if n:
                    report.append(f"  pattern {where}: {src} dropped, {n} notes lost")
            elif cell == Cell.BLANK:
                if n:
                    kept.append(src)
                    report.append(f"  pattern {where}: {src} is blank in the table but plays {n} notes: kept")
                else:
                    drop.append(src)               # silent here: not a live channel on its account
            else:
                kept.append(src)
        groups: list[dict] = []
        for fid, members in sorted(folds.items()):
            if len(members) < 2:
                report.append(f"  pattern {where}: fold {fid} has one channel ({members[0]}): kept")
                kept.append(members[0])
                continue
            primary, tried = choose_primary(ctx, members, patterns, named.get(fid))
            followers = [m for m in members if m != primary]
            g = {"primary": primary, "followers": followers}
            if kind_of(primary) == "DAC":
                g["cut_primary"] = True
                if bank:
                    g["bank"] = True
                if mix_note:
                    g["mix_note"] = mix_note
            groups.append(g)
            how = " (named in the table)" if fid in named else ""
            report.append(f"  pattern {where}, fold {fid} ({'+'.join(members)}): primary {primary}{how}")
            for cand, stats in tried:
                keys = set()
                for s in stats:
                    keys |= s.keys
                detail = "; ".join(
                    f"{s.follower} {s.paired} paired, {s.solo} solo, {s.lost} lost"
                    + (f", {s.cuts} cut the primary" if s.cuts else "")
                    for s in stats)
                mark = "*" if cand == primary else " "
                report.append(f"    {mark} {cand:5}: {detail}; {len(keys)} composites")
        blocks.append({"patterns": where, "drop": drop, "groups": groups, "keep": kept})
    # Patterns the song has that the table does not name, and the reverse
    last = ctx.last_pattern
    missing = sorted(set(range(last + 1)) - set(table))
    if missing:
        report.append(f"  the song has pattern{'s' if len(missing) > 1 else ''} "
                      f"{format_patterns(missing)} the table does not name: nothing folds there")
    extra = sorted(p for p in table if p > last)
    if extra:
        report.append(f"  the table names pattern{'s' if len(extra) > 1 else ''} {format_patterns(extra)}, "
                      f"past the song's last ({last:x})")
    return blocks, report


def render(blocks: list[dict], names: dict[str, str], csv_path: str) -> str:
    lines = [MARK_BEGIN.format(csv=csv_path.replace("\\", "/"))]
    if names:
        lines.append("# " + ", ".join(f"{src} = {name}" for src, name in names.items()))
    lines.append("merge_patterns:")
    for b in blocks:
        keep = f"keep {', '.join(b['keep'])}" if b["keep"] else "nothing kept"
        lines.append(f"  - patterns: \"{b['patterns']}\"          # {keep}")
        if b["drop"]:
            lines.append(f"    drop: [{', '.join(b['drop'])}]")
        if b["groups"]:
            lines.append("    groups:")
            for g in b["groups"]:
                lines.append(f"      - primary: {g['primary']}")
                lines.append(f"        followers: [{', '.join(g['followers'])}]")
                if g.get("cut_primary"):
                    lines.append("        cut_primary: true")
                if g.get("bank"):
                    lines.append("        bank: true")
                if g.get("mix_note"):
                    lines.append(f"        mix_note: {g['mix_note']}")
        else:
            lines.append("    groups: []")
    lines.append(MARK_END)
    return "\n".join(lines) + "\n"


def write_block(config_path: str, block: str) -> str:
    """Put the block into the config in place of the one written before, or at the end."""
    text = Path(config_path).read_text(encoding="utf-8")
    begin = text.find("# >>> merge_patterns")
    end = text.find(MARK_END)
    if begin >= 0 and end > begin:
        end = text.find("\n", end) + 1
        text = text[:begin] + block + text[end:]
        how = "replaced"
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n\n" + block
        how = "appended"
    Path(config_path).write_text(text, encoding="utf-8", newline="\n")
    return how


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("config")
    ap.add_argument("csv")
    ap.add_argument("--write", action="store_true", help="write the section into the config (between markers)")
    ap.add_argument("--bank", action="store_true",
                    help="bank: true on every drum-primary group: its mixes share slots as sample banks (9xx)")
    ap.add_argument("--mix-note", metavar="NOTE",
                    help="mix_note: NOTE on every drum-primary group: its mixes are made no higher than this "
                         "MOD note (F2: 11 kHz instead of a hat's 28 kHz)")
    args = ap.parse_args()
    cfg = ConversionConfig.from_yaml(args.config)
    columns, table, names = read_table(args.csv, cfg)
    ctx = survey_context(cfg, Path(args.config).resolve().parent)
    blocks, report = build(ctx, columns, table, bank=args.bank, mix_note=args.mix_note)
    print(f"{cfg.name}: {len(table)} patterns in the table, the song's last is {ctx.last_pattern:x}\n")
    print("\n".join(report))
    print()
    block = render(blocks, names, args.csv)
    if args.write:
        how = write_block(args.config, block)
        print(f"{how} the merge_patterns section in {args.config}")
    else:
        print(block)


if __name__ == "__main__":
    main()
