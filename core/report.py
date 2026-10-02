"""The conversion report convert.py prints: what was made, and how far to trust it.

Sections, in order:

    header    the song, the build, the source, tempo, settings and the file written
    Checks    one line per area (tempo, pitch, samples, merge, levels, patterns): a tick where
              nothing is wrong, else the count and each warning under it, with its fix
    Channels  the SMPS channels: notes, transpose, the MOD column they play on
    Samples   every slot: what it was rendered from, rate, size against the sample limit, loop,
              volume, the notes that play it, and its status
    Merge     (merged build) each fold: composites, notes folded / solo / lost / cut, slots
    Details   (--verbose) the converter's full notes: composites, bank sounds, loop extensions

The converter's `infos` and `warnings` are structured dicts; this module is the only place
they are turned into text.  The sample table reads the written file (core.sample_audit) for
the notes each sample plays, so it reports what the MOD does, not what was planned.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from .diagnostics import InfoKind, WarningKind
from .mod_limits import MAX_MOD_SAMPLE_BYTES
from .sample_audit import audit
from .tables import source_names, synth_note_name

OK = "[green]✓[/green]"
WARN = "[yellow]⚠[/yellow]"
BAD = "[red]✗[/red]"

_KIND_STYLE = {
    'FM': "cyan", 'PSG': "magenta", 'noise': "magenta", 'DAC': "blue",
    'chip': "bright_cyan", 'mix': "bright_magenta", 'bank': "bright_blue",
}

_CHECK_ORDER = ("tempo", "pitch", "samples", "merge", "levels", "patterns")

# A tempo whose whole-number BPM is this many percent off the driver's is flagged
_BPM_ERROR_PCT = 0.1

# Size bar: cells, and the share of the sample limit where it turns yellow / red
_BAR_CELLS = 8
_BAR_YELLOW = 0.5
_BAR_RED = 0.85


@dataclass
class Report:
    """Everything the report reads, gathered by convert.py once the file is written."""
    config: object
    song: object
    converter: object
    output_path: str
    output_bytes: int
    merged: bool
    verbose: bool
    synth: object
    psg_synth: object
    bpm: dict = field(default_factory=dict)       # derived, exact, error_pct, better (option or None)
    mod_channels: int = 0
    patterns: int = 0


# ── Warnings: (check, headline, fix) ──────────────────────────────────────────────────────────


def _ctx(w: dict) -> str:
    if w.get('extra_ctx'):
        return f" [dim]{escape(str(w['extra_ctx']))}[/dim]"
    if w.get('voice_idx') is not None:
        return f" [dim]voice ${w['voice_idx']:02X}[/dim]"
    return ""


def _w_clamp(w: dict):
    high = w['type'] == WarningKind.CLAMP_HIGH
    src = w['src_name']
    ctx = w.get('extra_ctx', '') or ''
    if ctx and not ctx.startswith('form '):
        where = f"psg_voice_map entry [bold]{escape(ctx)}[/bold]"
    elif ctx:
        where = f"psg_map entry [bold]{escape(ctx)}[/bold]"
    elif str(w.get('channel', '')).startswith('PSG'):
        where = "psg_voice_map or psg_map entry"
    else:
        where = "voice_map entry"
    fix = f"add a {where} with [cyan]{'low' if high else 'high'}: {src}[/cyan]"
    if w.get('psg_available_labels'):
        fix += f" [dim](no entry was active; check {', '.join(w['psg_available_labels'])})[/dim]"
    return ("pitch",
            f"[bold]{w.get('channel', '')}[/bold]{_ctx(w)} n{src} clamped to {'B3' if high else 'C1'}: notes "
            f"{'above' if high else 'below'} {w['boundary']} leave the MOD's three octaves", fix)


def _w_map_gap(w: dict):
    return ("pitch",
            f"[bold]{w.get('channel', '')}[/bold] voice ${w.get('voice_idx') or 0:02X}: n{w['note_name']} is in no "
            f"voice_map range [dim](they span {w['range_lo']}–{w['range_hi']})[/dim]",
            f"extend a range to [cyan]{w['note_name']}[/cyan] or add one")


def _w_missing_source(w: dict):
    return ("pitch", f"source [bold]{w['source']}[/bold] is not in the song",
            "check the channel's [cyan]source:[/cyan]")


def _w_rate3(w: dict):
    side = "above" if w['above'] else "below"
    fix = ("[cyan]tone2_n: 1[/cyan] if the channel plays nMaxPSG" if w['above']
           else "the [cyan]tone2_n:[/cyan] analyze.py prints, or a synth_root inside C3–Gs8")
    return ("samples", f"[bold]{escape(w['context'])}[/bold] rate-3 noise synth_root {w['synth_root']} is {side} "
                       f"the driver's PSG table: no note clocks the LFSR there", fix)


def _w_tempo_no_slot(w: dict):
    return ("tempo", f"tempo change at {w['pattern']:02X}:{w['row']:02d} (BPM {w['bpm']}) found no free effect "
                     f"slot and is not written", "[cyan]num_mod_channels:[/cyan] one step up gives it a channel")


def _w_tempo_bpm_range(w: dict):
    return ("tempo", f"tempo change at {w['pattern']:02X}:{w['row']:02d} needs BPM {w['exact_bpm']:.1f}, "
                     f"clamped to {w['bpm']}",
            "a larger [cyan]ticks_per_row:[/cyan] or smaller [cyan]target_speed:[/cyan]")


def _w_pattern_overflow(w: dict):
    return ("patterns", f"[bold]{w.get('channel', '')}[/bold] runs past max_patterns ({w['max']}) at pattern "
                        f"{w['pattern']}: truncated", "raise [cyan]max_patterns:[/cyan]")


def _w_rest_no_slot(w: dict):
    return ("patterns", f"[bold]{w['channel']}[/bold] starts with a rest but row 0 has no slot for its C00: "
                        f"the last note before the loop rings through it",
            "[cyan]num_mod_channels:[/cyan] one step up gives the tempo commands a channel")


def _w_loop_no_slot(w: dict):
    eff, par = w['overwrote']
    what = f" (it replaced {eff:X}{par:02X})" if (eff, par) != (0, 0) else ""
    return ("patterns", f"the loop's Bxx at {w['pattern']:02X}:{w['row']:02d} found no free effect slot and "
                        f"went on channel 1{what}",
            "[cyan]num_mod_channels:[/cyan] one step up gives it a channel")


def _w_sustain_short(w: dict):
    limit = {'mod': f"{w['max_kb']} KB at {w['rate'] / 1000:.1f} kHz", 'cap': "the 10 s auto cap",
             'setting': "sustain_duration"}[w['limit']]
    fix = {'mod': "a lower [cyan]root:[/cyan] halves the bytes per second per octave",
           'setting': "[cyan]sustain_duration: auto[/cyan]", 'cap': None}[w['limit']]
    return ("samples", f"[bold]{w['kind']} {w['instrument']}[/bold] falls silent early: a note needs "
                       f"{w['need']:.2f} s, the sample holds {w['have']:.2f} s [dim]({limit})[/dim]", fix)


def _w_truncated(w: dict):
    return ("samples", f"[bold]inst {w['instrument']}[/bold] rendered {w['bytes'] / 1024:.1f} KB, cut to the "
                       f"{w['max_bytes'] / 1024:.0f} KB limit", None)


def _w_noise_envelopes(w: dict):
    others = ", ".join(f"{k} ×{n}" for k, n in sorted(w['others'].items(), key=lambda kv: -kv[1]))
    return ("samples", f"[bold]noise {w['instrument']}[/bold] is rendered with {w['envelope']}; also played "
                       f"with {others}", "[cyan]envelopes: {label: free slot}[/cyan] on the psg_map entry")


def _w_synth_root_ambiguous(w: dict):
    votes = ", ".join(f"{synth_note_name(d)} ×{n}" for d, n in sorted(w['votes'].items(), key=lambda kv: -kv[1]))
    return ("pitch", f"[bold]inst {w['instrument']}[/bold] {escape(w['context'])}: its low note plays at "
                     f"{votes}; rendered at {synth_note_name(w['synth_root'])}, the others out of tune",
            "[cyan]range_space: chip[/cyan] (tools/config_to_chip_space.py) or split the entry")


def _w_detune_no_slot(w: dict):
    parts = ", ".join(f"inst {inst} {d:+d} ×{n}" for (inst, d), n in sorted(w['unplaced'].items(), key=lambda kv: -kv[1]))
    return ("pitch", f"{len(w['unplaced'])} smpsAlterNote detunes have no free slot and play their "
                     f"instrument's own sample: {parts}", "free an instrument slot")


def _lost_parts(w: dict) -> list[str]:
    parts = []
    for key, what in (('orphans', "start under the primary"), ('held', "rings cut by a primary note"),
                      ('truncated', "cut by the primary's rest"), ('solo_cut', "solo, cut by the next note")):
        if w.get(key):
            parts.append(f"{w[key]} {what}")
    return parts


def _w_merge_lost(w: dict):
    parts = _lost_parts(w)
    head = (f"[bold]{w['primary']}+{w['follower']}[/bold][dim]{escape(w.get('where', ''))}[/dim] "
            f"of {w['follower']}'s {w['notes']} notes: " + (" · ".join(parts) if parts else "all fold"))
    if w.get('vibrato'):
        head += f" [dim]· {w['vibrato']} modulate differently (the primary's vibrato plays)[/dim]"
    return ("merge", head, None)


def _w_merge_headroom(w: dict):
    worst: dict[int, float] = {}
    for inst, db in w['instruments']:
        worst[inst] = max(worst.get(inst, 0.0), db)
    parts = ", ".join(f"{inst} {db:.1f} dB" for inst, db in sorted(worst.items()))
    return ("levels", f"{len(worst)} composites sum past full scale and play that much quieter: {parts}", None)


def _w_merge_unsupported(w: dict):
    head = (f"[bold]{w['primary']}[/bold]: {w['count']} composites ({w['notes']} notes) have no slot "
            f"[dim]({w['reason']})[/dim]: the primary plays alone there")
    if w.get('stand_ins'):
        head += f" [dim]· {w['stand_ins']} more use a same-shape stand-in[/dim]"
    return ("merge", head, "lower a group's [cyan]max_composites[/cyan] or free a slot")


def _w_merge_missing(w: dict):
    return ("samples", f"composite {w['instrument']}: instrument {w['missing']} has no sample to mix", None)


def _w_merge_fill_lost(w: dict):
    return ("merge", f"[bold]{w['source']}[/bold] fill pool: {w['lost']} of {w['notes']} notes found no "
                     f"silent channel", None)


def _w_merge_dropped(w: dict):
    pats = ", ".join(f"{p:x}" for p in w['patterns'])
    return ("merge", f"[bold]{w['channel']}[/bold]: {w['notes']} notes lost in pattern{'s' if len(w['patterns']) != 1 else ''} "
                     f"{pats}: no group folds it and it has no column", "a [cyan]keep[/cyan] there gives it one")


def _w_merge_bank_dropped(w: dict):
    return ("merge", f"[bold]{w['primary']}[/bold] bank: a sound for {w['notes']} notes left out ({w['reason']}) "
                     f"[dim]{escape(w['detail'])}[/dim]", "raise [cyan]merge_bank_slots[/cyan]")


def _w_merge_bank_idle(w: dict):
    slots = ", ".join(str(s) for s in w['slots'])
    return ("merge", f"bank slot{'s' if len(w['slots']) > 1 else ''} {slots} unused while {w['dropped']} "
                     f"composites had none", f"[cyan]merge_bank_slots: {w['banks']}[/cyan]")


def _w_merge_unspecified(w: dict):
    pats = ", ".join(f"{p:x}" for p in w['patterns'])
    return ("merge", f"no merge_patterns block names pattern{'s' if len(w['patterns']) != 1 else ''} {pats}: "
                     f"nothing folds there", None)


_WARNINGS: dict[WarningKind, Callable[[dict], tuple[str, str, str | None]]] = {
    WarningKind.CLAMP_HIGH: _w_clamp, WarningKind.CLAMP_LOW: _w_clamp, WarningKind.MAP_GAP: _w_map_gap,
    WarningKind.MISSING_SOURCE: _w_missing_source, WarningKind.RATE3_SYNTH_ROOT: _w_rate3,
    WarningKind.TEMPO_NO_SLOT: _w_tempo_no_slot, WarningKind.TEMPO_BPM_RANGE: _w_tempo_bpm_range,
    WarningKind.PATTERN_OVERFLOW: _w_pattern_overflow, WarningKind.REST_NO_SLOT: _w_rest_no_slot, WarningKind.LOOP_NO_SLOT: _w_loop_no_slot,
    WarningKind.SUSTAIN_SHORT: _w_sustain_short, WarningKind.SAMPLE_TRUNCATED: _w_truncated,
    WarningKind.NOISE_ENVELOPES: _w_noise_envelopes, WarningKind.SYNTH_ROOT_AMBIGUOUS: _w_synth_root_ambiguous,
    WarningKind.DETUNE_NO_SLOT: _w_detune_no_slot,
    WarningKind.MERGE_LOST: _w_merge_lost, WarningKind.MERGE_HEADROOM: _w_merge_headroom,
    WarningKind.MERGE_UNSUPPORTED: _w_merge_unsupported, WarningKind.MERGE_MISSING_SAMPLE: _w_merge_missing,
    WarningKind.MERGE_FILL_LOST: _w_merge_fill_lost, WarningKind.MERGE_DROPPED: _w_merge_dropped,
    WarningKind.MERGE_BANK_DROPPED: _w_merge_bank_dropped, WarningKind.MERGE_BANK_IDLE: _w_merge_bank_idle,
    WarningKind.MERGE_UNSPECIFIED: _w_merge_unspecified,
}


def warning_lines(warnings: list[dict]) -> dict[str, list[tuple[str, str | None]]]:
    """{check: [(headline, fix)]} for the converter's warnings; an unknown type is shown raw."""
    out: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    folds = [w for w in warnings if w['type'] == WarningKind.MERGE_LOST]
    if folds:
        parts = []
        for w in folds:
            lost, cut = w.get('orphans', 0), w.get('held', 0) + w.get('truncated', 0) + w.get('solo_cut', 0)
            nums = " ".join(x for x in (f"[red]{lost} lost[/red]" if lost else "",
                                        f"[yellow]{cut} cut[/yellow]" if cut else "") if x)
            parts.append(f"[bold]{w['primary']}+{w['follower']}[/bold][dim]{escape(w.get('where', ''))}[/dim] "
                         + (nums or "[dim]vibrato differs[/dim]"))
        out['merge'].append((" · ".join(parts), None))
    for w in warnings:
        if w['type'] == WarningKind.MERGE_LOST:
            continue
        fn = _WARNINGS.get(w['type'])
        if fn is None:
            out['other'].append((escape(str(w)), None))
            continue
        check, head, fix = fn(w)
        out[check].append((head, fix))
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────────────────────


def _infos(rep: Report, kind: InfoKind) -> list[dict]:
    return [i for i in rep.converter.infos if i['type'] == kind]


def _kb(n: float) -> str:
    return f"{n / 1024:.1f}K" if n < 100 * 1024 else f"{n / 1024:.0f}K"


def _bar(frac: float, width: int = _BAR_CELLS) -> Text:
    """A block bar of `frac` (0..1) of `width` cells: green, yellow from _BAR_YELLOW of the
    limit, red from _BAR_RED."""
    frac = max(0.0, min(1.0, frac))
    eighths = round(frac * width * 8)
    full, part = divmod(eighths, 8)
    s = "█" * full + ("" if not part else " ▏▎▍▌▋▊▉"[part])
    style = "green" if frac < _BAR_YELLOW else "yellow" if frac < _BAR_RED else "red"
    t = Text(s, style=style)
    t.append("·" * (width - len(s)), style="bright_black")
    return t


def _secs(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def _label_row(label: str, *lines: str) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold", justify="right", width=8, no_wrap=True)
    t.add_column()
    for k, line in enumerate(lines):
        if line:
            t.add_row(label if k == 0 else "", line)
    return t


def _section(console: Console, title: str, subtitle: str = "") -> None:
    console.print()
    line = Text()
    line.append(f" {title} ", style="bold black on bright_yellow")
    if subtitle:
        line.append(f"  {subtitle}", style="dim")
    console.print(line)


# ── Header ────────────────────────────────────────────────────────────────────────────────────


def print_header(console: Console, rep: Report) -> None:
    cfg, song = rep.config, rep.song
    build = (f"merged build · {rep.mod_channels} channels" if rep.merged
             else f"reference build · {rep.mod_channels} channels")
    console.rule(f"[bold bright_white]{escape(cfg.name)}[/bold bright_white]  [dim]{build}[/dim]",
                 style="bright_black")
    kinds = [ch.header.channel_type for ch in song.channels]
    parts = [p for p in ("DAC" if "DAC" in kinds else "",
                         f"{kinds.count('FM')} FM" if "FM" in kinds else "",
                         f"{kinds.count('PSG')} PSG" if "PSG" in kinds else "") if p]
    source = (f"[cyan]{escape(cfg.input_file)}[/cyan]  [dim]{escape(song.header.voice_label or '')} · "
              f"{' · '.join(parts)}[/dim]")
    b = rep.bpm
    tempo = (f"div {song.header.tempo_divider} · mod {song.header.tempo_modifier} · {cfg.region.upper()}  [dim]→[/dim]  "
             f"[bold]{cfg.target_bpm}[/bold] BPM · speed [bold]{cfg.target_speed}[/bold] · "
             f"{cfg.ticks_per_row} ticks/row")
    if b.get('exact') and abs(b.get('error_pct', 0.0)) >= _BPM_ERROR_PCT:
        tempo += f"  {WARN} [yellow]{b['error_pct']:+.2f} %[/yellow]"
    elif b.get('exact'):
        tempo += f"  {OK}"
    fm = (f"FM {rep.synth.mode}" if rep.synth and rep.synth.enabled else "FM from disk")
    psg = "PSG synthesised" if rep.psg_synth and rep.psg_synth.enabled else "PSG from disk"
    s = rep.synth or rep.psg_synth
    settings = f"{fm} · {psg}"
    if s is not None:
        settings += f" · {s.max_sample_kb} KB per sample · loops {s.sustain_loops}"
        if s.treble_shelf_db:
            settings += f" · treble {s.treble_shelf_db:+g} dB above {s.treble_shelf_hz:g} Hz"
    if rep.synth is not None:
        settings += f" · legato {rep.synth.legato} · player {rep.synth.player}"
    loop = next((i for i in rep.converter.infos if i['type'] == InfoKind.LOOP_SET), None)
    sample_bytes = sum(len(sm.data) for sm in rep.converter.mod.samples if sm is not None)
    out = (f"[cyan]{escape(rep.output_path)}[/cyan]  [bold]{rep.output_bytes / 1024:.0f} KB[/bold]  "
           f"[dim]samples {sample_bytes / 1024:.0f} K · patterns {(rep.output_bytes - sample_bytes) / 1024:.0f} K · "
           f"{rep.patterns} patterns" + (f" · loop → {loop['target']}" if loop else "") + "[/dim]")
    console.print(_label_row("Source", source))
    console.print(_label_row("Tempo", tempo))
    console.print(_label_row("Settings", f"[dim]{settings}[/dim]"))
    console.print(_label_row("Output", out))


# ── Checks ────────────────────────────────────────────────────────────────────────────────────


def _check_summaries(rep: Report, rows: list[dict], sources: dict[int, dict]) -> dict[str, tuple[str, str]]:
    """{check: (status markup, summary)} for the areas that are clean or carry only notes."""
    out: dict[str, tuple[str, str]] = {}
    b = rep.bpm
    changes = len(_infos(rep, InfoKind.TEMPO_CHANGE))
    extra = f" · {changes} tempo change{'s' if changes != 1 else ''} (Fxx)" if changes else ""
    if b.get('exact') and abs(b.get('error_pct', 0.0)) >= _BPM_ERROR_PCT:
        better = b.get('better')
        hint = (f" [dim]→ target_speed: {better['speed']} gives {better['bpm']} ({better['error_pct']:+.2f} %)[/dim]"
                if better else "")
        out['tempo'] = (WARN, f"{b['exact']:.3f} BPM rounded to {rep.config.target_bpm}: "
                              f"{b['error_pct']:+.2f} %{hint}{extra}")
    else:
        out['tempo'] = (OK, f"{rep.config.target_bpm} BPM, the driver's tempo exactly{extra}")
    notes = sum(1 for ch in rep.song.channels for e in ch.events if e.is_note and not e.note.is_rest)
    out['pitch'] = (OK, f"{notes} notes, every one inside the MOD's three octaves and mapped")
    synth = [r for r in rows if sources.get(r['inst'], {}).get('kind') in ('FM', 'PSG', 'noise', 'chip', 'mix')]
    looped = sum(1 for r in synth if r['loop'])
    biggest = max(rows, key=lambda r: r['bytes'], default=None)
    limit = (rep.synth or rep.psg_synth).max_sample_bytes if (rep.synth or rep.psg_synth) else MAX_MOD_SAMPLE_BYTES
    msg = f"every note fits its sample · {looped} of {len(synth)} synthesised looped"
    if biggest is not None:
        msg += f" · largest {biggest['bytes'] / 1024:.0f} K of {limit / 1024:.0f} K (inst {biggest['inst']})"
    out['samples'] = (OK, msg)
    if rep.merged:
        out['merge'] = (OK, "every follower note folds, plays solo or fills a silent channel")
    return out


def print_checks(console: Console, rep: Report, lines: dict, rows: list[dict], sources: dict[int, dict]) -> None:
    total = sum(len(v) for v in lines.values())
    _section(console, "Checks", f"{total} warning{'s' if total != 1 else ''}" if total else "all clear")
    summaries = _check_summaries(rep, rows, sources)
    t = Table.grid(padding=(0, 1))
    t.add_column(width=2)
    t.add_column(style="bold", width=9, no_wrap=True)
    t.add_column()
    for check in (*_CHECK_ORDER, *(k for k in lines if k not in _CHECK_ORDER)):
        items = lines.get(check, [])
        if not items and check not in summaries:
            continue
        if not items:
            mark, text = summaries[check]
            t.add_row(mark, check, f"[dim]{text}[/dim]" if mark == OK else text)
            continue
        t.add_row(WARN, check, f"[yellow]{len(items)} warning{'s' if len(items) != 1 else ''}[/yellow]")
        for head, fix in items:
            t.add_row("", "", f"[bright_black]•[/bright_black] {head}")
            if fix:
                t.add_row("", "", f"  [green]fix[/green] [dim]{fix}[/dim]")
    console.print(Padding(t, (0, 0, 0, 1), expand=False))


# ── Channels ──────────────────────────────────────────────────────────────────────────────────


def print_columns(console: Console, rep: Report, rows: list[dict]) -> None:
    """The merged build's MOD columns: the notes each plays and the instruments they play."""
    cols: dict[int, dict[int, int]] = defaultdict(dict)
    for r in rows:
        for c, n in r.get('channel_notes', {}).items():
            cols[c][r['inst']] = n
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold dim", padding=(0, 2, 0, 0))
    t.add_column("Col", justify="right", style="bold cyan")
    t.add_column("Notes", justify="right")
    t.add_column("Instruments (notes)", style="dim")
    for c in sorted(cols):
        insts = sorted(cols[c].items(), key=lambda kv: -kv[1])
        t.add_row(str(c), str(sum(cols[c].values())), " ".join(f"{i}[bright_black]×{n}[/bright_black]" for i, n in insts))
    _section(console, "Columns", f"{len(rep.song.channels)} SMPS tracks folded onto {rep.mod_channels} MOD columns")
    console.print(Padding(t, (0, 0, 0, 2), expand=False))


def print_channels(console: Console, rep: Report, rows: list[dict]) -> None:
    if rep.merged:
        print_columns(console, rep, rows)
        return
    cfg, song = rep.config, rep.song
    by_source = {c.source: c for c in cfg.channels}
    by_col: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        for c in r['channels']:
            by_col[c].append(r['inst'])
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold dim", padding=(0, 2, 0, 0))
    t.add_column("Channel", style="bold cyan", no_wrap=True)
    t.add_column("Notes", justify="right")
    if any(c.transpose for c in cfg.channels):
        t.add_column("Transpose", justify="right", style="dim")
    t.add_column("Column", justify="right")
    t.add_column("Instruments", style="dim")
    for ch, name in zip(song.channels, source_names(song), strict=True):
        notes = sum(1 for e in ch.events if e.is_note and not e.note.is_rest)
        c = by_source.get(name)
        cells = [name, str(notes)]
        if any(x.transpose for x in cfg.channels):
            cells.append(f"{c.transpose:+d}" if c and c.transpose else "")
        col = c.mod_channel + 1 if c else None
        cells += [str(col) if col else "[dim]—[/dim]",
                  " ".join(str(i) for i in sorted(set(by_col.get(col, [])))) if col else ""]
        t.add_row(*cells)
    _section(console, "Channels", f"{len(song.channels)} SMPS tracks → {rep.mod_channels} MOD columns")
    console.print(Padding(t, (0, 0, 0, 2), expand=False))


# ── Samples ───────────────────────────────────────────────────────────────────────────────────


def sample_flags(rows: list[dict], warnings: list[dict]) -> dict[int, list[tuple[str, str]]]:
    """{instrument: [(severity, text)]}: the converter's sample warnings and the audit's flags
    (its "too short" left out: it cannot see note fills or a drum's own length)."""
    out: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for w in warnings:
        if w['type'] == WarningKind.SUSTAIN_SHORT:
            out[w['instrument']].append(("warn", f"short {w['need'] - w['have']:.2f} s"))
        elif w['type'] == WarningKind.SAMPLE_TRUNCATED:
            out[w['instrument']].append(("warn", "cut at the limit"))
    for r in rows:
        for f in r['flags']:
            if f == "unused":
                out[r['inst']].append(("warn", f"unused ({r['bytes'] / 1024:.1f} K no note plays)"))
            elif f == "empty slot":
                out[r['inst']].append(("bad", "empty slot: its notes are silent"))
            elif f.startswith("low rate"):
                out[r['inst']].append(("info", f.replace(" Hz", "").replace("low rate (", "low rate ").rstrip(")")))
            elif f.startswith("same as") or f.startswith("finetune variant"):
                out[r['inst']].append(("info", f.split(" (")[0].replace("finetune variant of", "finetune of")))
    return out


def print_samples(console: Console, rep: Report, rows: list[dict], sources: dict[int, dict],
                  flags: dict[int, list[tuple[str, str]]]) -> None:
    s = rep.synth or rep.psg_synth
    limit = s.max_sample_bytes if s is not None else MAX_MOD_SAMPLE_BYTES
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold dim", padding=(0, 1, 0, 0))
    t.add_column("#", justify="right", style="bold")
    t.add_column("Kind", no_wrap=True)
    t.add_column("Source", no_wrap=True, max_width=34, overflow="ellipsis")
    t.add_column("Rate", justify="right")
    t.add_column("Size", justify="right")
    t.add_column("", no_wrap=True)
    t.add_column("Loop", no_wrap=True)
    if rep.verbose:
        t.add_column("Release", justify="right", style="dim")
    t.add_column("Vol", justify="right", style="dim")
    t.add_column("Notes", justify="right")
    if rep.verbose:
        t.add_column("Heard", justify="right", style="dim")
    t.add_column("Range (FT2)", no_wrap=True, style="dim")
    t.add_column("Status", no_wrap=True)
    total = looped = 0
    for r in rows:
        src = sources.get(r['inst'], {})
        kind = src.get('kind', "file")
        rate = src.get('rate')
        if rate is None and r.get('secs'):
            rate = r['bytes'] / r['secs']
        total += r['bytes']
        if r['loop']:
            looped += 1
            start, length = r['loop']
            loop = (f"{_secs(1000 * length / rate)} [dim]@ {_secs(1000 * start / rate)}[/dim]" if rate
                    else f"{length} [dim]@ {start}[/dim]")
        elif kind == 'bank':
            loop = "[dim]9xx ×{}[/dim]".format(len(r.get('sounds') or []))
        else:
            loop = "[bright_black]—[/bright_black]"
        name = src.get('source') or r['name']
        status_parts = []
        worst = "ok"
        for sev, text in flags.get(r['inst'], []):
            style = {"bad": "red", "warn": "yellow", "info": "dim"}[sev]
            status_parts.append(f"[{style}]{escape(text)}[/{style}]")
            if sev == "bad" or (sev == "warn" and worst != "bad"):
                worst = sev
        mark = {"ok": OK, "warn": WARN, "bad": BAD}[worst]
        rng = f"{r['range'][0]}–{r['range'][1]}" if r['range'] else ""
        if r['range'] and r['range'][0] == r['range'][1]:
            rng = r['range'][0]
        cells = [str(r['inst']), f"[{_KIND_STYLE.get(kind, 'white')}]{kind}[/]", escape(name),
                 f"{rate / 1000:.1f}k" if rate else "", _kb(r['bytes']), _bar(r['bytes'] / limit), loop]
        if rep.verbose:
            rel = src.get('release')
            cells.append(f"{rel:.0f} dB/s" if rel else "")
        cells += [str(r['volume']), str(r['notes']) if r['notes'] else "[yellow]0[/yellow]"]
        if rep.verbose:
            cells.append(f"{r['play_share'] * 100:.0f} %")
        cells += [rng, f"{mark} " + " · ".join(status_parts)]
        t.add_row(*cells)
    sub = (f"{len(rows)} slots · {total / 1024:.0f} K · {looped} looped · bar = share of the "
           f"{limit / 1024:.0f} K sample limit")
    _section(console, "Samples", sub)
    console.print(Padding(t, (0, 0, 0, 2), expand=False))


# ── Merge ─────────────────────────────────────────────────────────────────────────────────────


def _where(label: str) -> tuple[str, str]:
    """("DAC+FM2+PSG3", "1-4") from "DAC+FM2+PSG3 [1-4]"."""
    if label.endswith("]") and " [" in label:
        head, _, pats = label.rpartition(" [")
        return head, pats[:-1]
    return label, ""


def print_merge(console: Console, rep: Report) -> None:
    cfg = rep.config
    home = {c.source: c.mod_channel + 1 for c in cfg.channels}
    lost: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(int))
    for w in rep.converter.warnings:
        if w['type'] == WarningKind.MERGE_LOST:
            d = lost[(w['primary'], w.get('where', '').strip(" []"))]
            d['lost'] += w.get('orphans', 0)
            d['cut'] += w.get('held', 0) + w.get('truncated', 0) + w.get('solo_cut', 0)
    rows = []
    for g in _infos(rep, InfoKind.MERGE_GROUP):
        _name, pats = _where(g['label'])
        col = g['route'] + 1 if g.get('route') is not None else home.get(g['primary'])
        if not g.get('followers'):
            rows.append((pats, col, f"[bold]{g['primary']}[/bold] [dim]moved[/dim]", "", "", "", "", ""))
            continue
        d = lost.get((g['primary'], pats), {})
        comps = str(len(g['composites']))
        if g.get('unison'):
            comps += f" [dim]+{g['unison']['notes']} unison[/dim]"
        fold = f"[bold]{g['primary']}[/bold] + " + " ".join(g['followers'])
        rows.append((pats, col, fold, comps, str(g['paired']), str(g.get('solo') or ""),
                     f"[red]{d['lost']}[/red]" if d.get('lost') else "[dim]—[/dim]",
                     f"[yellow]{d['cut']}[/yellow]" if d.get('cut') else "[dim]—[/dim]"))
    for f in _infos(rep, InfoKind.MERGE_FILL):
        where = ", ".join(f"{n}→{ch}" for ch, n in sorted(f['targets'].items(), key=lambda kv: -kv[1]))
        rows.append(("", "", f"[bold]{f['source']}[/bold] [dim]fill: {where}[/dim]", "", str(f['placed']), "",
                     f"[red]{f['lost']}[/red]" if f['lost'] else "[dim]—[/dim]",
                     f"[yellow]{f['cut']}[/yellow]" if f['cut'] else "[dim]—[/dim]"))
    blocks = any(r[0] for r in rows)            # merge_patterns: blocks; a song-wide merge has none
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold dim", padding=(0, 2, 0, 0))
    if blocks:
        t.add_column("Patterns", style="dim", no_wrap=True)
    t.add_column("Col", justify="right")
    t.add_column("Fold", no_wrap=True)
    t.add_column("Composites", justify="right")
    t.add_column("Folded", justify="right")
    t.add_column("Solo", justify="right")
    t.add_column("Lost", justify="right")
    t.add_column("Cut", justify="right")
    for r in sorted(rows, key=lambda r: (_pat_key(r[0]) if r[0] else (999, ""), r[1] or 99)):
        t.add_row(*((r[0],) if blocks else ()), str(r[1] or ""), *r[2:])
    slots = next(iter(_infos(rep, InfoKind.MERGE_SLOTS)), None)
    sub = ""
    if slots:
        gone = slots['wanted'] - slots['used'] - slots.get('banked', 0) - slots.get('stand_ins', 0)
        sub = (f"composite slots {slots['used']}/{slots['free']} · {slots['wanted']} wanted · "
               f"{slots.get('banked', 0)} in banks")
        if slots.get('stand_ins'):
            sub += f" · {slots['stand_ins']} stand-ins"
        if gone > 0:
            sub += f" · {gone} dropped"
    _section(console, "Merge", sub)
    console.print(Padding(t, (0, 0, 0, 2), expand=False))
    notes = [f"{i['notes']} notes play from a bank ({i['cuts']} cut where their sound ends)"
             for i in _infos(rep, InfoKind.MERGE_BANK_NOTES)]
    notes += [f"instrument{'s' if len(i['instruments']) != 1 else ''} {', '.join(str(x) for x in i['instruments'])} "
              f"not rendered (no note of this build plays {'them' if len(i['instruments']) != 1 else 'it'})"
              for i in _infos(rep, InfoKind.MERGE_UNUSED)]
    notes += [f"rebuilt with merge_bank_slots {i['banks']} (slot {', '.join(map(str, i['slots']))} was idle)"
              for i in _infos(rep, InfoKind.MERGE_BANK_RETRY)]
    notes += [f"merge_bank_slots: auto → {i['reserve']} held back for {i['banks']} bank{'s' if i['banks'] != 1 else ''}"
              + (f" (built {i['passes']} times)" if i['passes'] > 1 else "")
              for i in _infos(rep, InfoKind.MERGE_BANK_SLOTS)]
    notes += [f"{i['cxx_moved']} banked notes' Cxx moved a row later (the attack row holds the 9xx)"
              for i in _infos(rep, InfoKind.MERGE_BANK_NOTES) if i.get('cxx_moved')]
    notes += [f"{i['after']}-channel MOD: columns {i['after'] + 1}–{i['before']} were empty" for i in _infos(rep, InfoKind.NARROWED)]
    notes += [f"limit_db: {i['composites']} mixes' peaks limited, up to {i['max_db']:.1f} dB"
              for i in _infos(rep, InfoKind.MERGE_LIMITED)]
    for n in notes:
        console.print(Padding(Text(n, style="dim"), (0, 0, 0, 2), expand=False))


def _pat_key(p: str) -> tuple[int, str]:
    try:
        return (int(p.split("-")[0], 16), p)
    except ValueError:
        return (-1, p)


# ── Details (--verbose) ───────────────────────────────────────────────────────────────────────


def detail_lines(infos: list[dict]) -> list[str]:
    """The converter's notes in full, for --verbose."""
    out: list[str] = []
    for info in infos:
        t = info['type']
        if t == InfoKind.LOOP_EXTENDED:
            out.append(f"[dim]{info['label']}[/dim] loop extended {info['before']} → {info['after']} events")
        elif t == InfoKind.RATE3_DIVIDER:
            out.append(f"rate-3 noise inst {info['instrument']}: tone-2 divider {info['n']} "
                       f"[dim](n{info['note']} {info['transpose']:+d} in the driver's PSG table)[/dim]")
        elif t == InfoKind.TEMPO_CHANGE:
            out.append(f"tempo change at {info['pattern']:02X}:{info['row']:02d}: modifier {info['modifier']} → "
                       f"BPM {info['bpm']} [dim](exact {info['exact_bpm']:.2f})[/dim]")
        elif t == InfoKind.TEMPO_DIV_CHANGE:
            out.append(f"duration divider {info['divider']} for every track from row {info['row']}")
        elif t == InfoKind.VIBRATO_RATE_LIMIT:
            out.append(f"vibrato {info['channel']}: {info['wanted_cycle_frames']}-frame cycle is faster than 4xy "
                       f"plays ({info['played_cycle_frames']:.1f})")
        elif t == InfoKind.SYNTH_ROOTS:
            out.append(f"synthesis pitches: {info['derived']} derived, {info['stated']} stated")
        elif t == InfoKind.DETUNE_VARIANTS:
            own = ", ".join(f"inst {i} {d:+d}" for i, d in sorted(info['own'].items()))
            if own:
                out.append(f"detune rendered into the sample: {own}")
            for inst, base, d, n in info['variants']:
                out.append(f"detune variant inst {inst}: inst {base} at {d:+d} FNUM [dim]({n} notes)[/dim]")
        elif t == InfoKind.DETUNE_TIES:
            out.append(f"ties retuned to their new detune: {info['placed']} E1x / E2x"
                       + (f" [dim]({info['skipped']} rows had no free effect slot)[/dim]" if info['skipped'] else ""))
        elif t == InfoKind.SYNTH_SHIFT:
            out.append(f"{escape(info['context'])}: rendered at {synth_note_name(info['synth_root'])}, "
                       f"{info['shift']:+d} semitones from its root (the rate carries it)")
        elif t == InfoKind.NOISE_ENVELOPE:
            out.append(f"noise inst {info['instrument']}: envelope {info['envelope']} "
                       f"[dim]({'derived' if info['derived'] else 'stated'}, {info['notes']} notes)[/dim]")
        elif t in (InfoKind.AUTO_SUSTAIN_FM, InfoKind.AUTO_SUSTAIN_PSG):
            kind = 'FM' if t == InfoKind.AUTO_SUSTAIN_FM else 'PSG'
            out.append(f"auto sustain {kind}: {info['shortest']}–{info['secs']} s over {info.get('instruments', 0)}")
        elif t == InfoKind.MERGE_GROUP and info.get('followers'):
            out.append(f"[bold]{escape(info['label'])}[/bold]: {info['paired']} folded into "
                       f"{len(info['composites'])} composites, {info['alone']} alone, {info.get('solo', 0)} solo")
            if info.get('unison'):
                u = info['unison']
                gains = ", ".join(f"{db:+.1f} dB ×{n}" for db, n in sorted(u['gains'].items()))
                out.append(f"  [dim]unison {u['notes']:3d} notes: the primary's own instrument, {gains}[/dim]")
            for inst, notes, detail, made_for, others in info['composites']:
                share = f" (made for {made_for})" if made_for else f" (also {', '.join(others)})" if others else ""
                out.append(f"  [dim]inst {escape(str(inst)):>7} {notes:3d} notes  {escape(detail)}{escape(share)}[/dim]")
        elif t == InfoKind.MERGE_BANK:
            out.append(f"bank slot {info['slot']}: {len(info['members'])} sounds, {info['bytes'] / 1024:.1f} K "
                       f"at volume {info['volume']}")
            for offset, size, notes, detail in info['members']:
                out.append(f"  [dim]9{offset >> 8:02X} {size:6d} bytes {notes:3d} notes  {escape(detail)}[/dim]")
        elif t == InfoKind.MERGE_FOLDS:
            out.append(f"[dim]{escape(info['pair'])}: {escape(info['what'])}[/dim]")
        elif t == InfoKind.DAC_SATURATED:
            out.append(f"{info['name']} (inst {info['instrument']}): soft-clipped, body {info['db']:+g} dB at the same peak")
        elif t == InfoKind.MERGE_UNISON_VOLUME:
            out.append(f"[dim]unison: inst {info['instrument']} baked {info['db']:+.1f} dB (volume {info['volume']})[/dim]")
    return out


# ── The whole report ──────────────────────────────────────────────────────────────────────────


def print_report(console: Console, rep: Report) -> None:
    rows, _notes = audit(rep.output_path)
    sources = rep.converter.sample_sources()
    flags = sample_flags(rows, rep.converter.warnings)
    lines = warning_lines(rep.converter.warnings)
    # The audit's flags on the written file (an unused or empty slot) count as sample warnings;
    # the converter's own sample warnings are listed already
    for inst, fl in sorted(flags.items()):
        for sev, text in fl:
            if sev in ("warn", "bad") and not text.startswith(("short ", "cut at")):
                lines['samples'].append((f"[bold]inst {inst}[/bold] {text}", None))
    print_header(console, rep)
    print_checks(console, rep, lines, rows, sources)
    print_channels(console, rep, rows)
    print_samples(console, rep, rows, sources, flags)
    if rep.merged:
        print_merge(console, rep)
    if rep.verbose:
        _section(console, "Details")
        console.print(Padding(Group(*(Text.from_markup(x) for x in detail_lines(rep.converter.infos))), (0, 0, 0, 2), expand=False))
    console.print()
    size = os.path.getsize(rep.output_path) if os.path.exists(rep.output_path) else rep.output_bytes
    total = sum(len(v) for v in lines.values())
    mark = OK if not total else WARN
    console.print(f" {mark} [bold]{'done' if not total else f'{total} warning' + ('s' if total != 1 else '')}"
                  f"[/bold]  [dim]{size:,} bytes written[/dim]")
    console.print()
