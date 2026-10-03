"""compare_songs' result as text (tools/vgm_lift.py, tools/rom_import.py): per channel its note
count and differences, one difference made again and again printed once with its count."""

from __future__ import annotations

from ..audio import pitch_name
from ..chips import MD_FM_CLOCK, MD_PSG_CLOCK, fm_frequency_hz, psg_frequency_hz
from ..smps import Aspect, ChannelDiff, NoteDiff, SongDiff

_FNUM_BITS = 11


def diff_counts(diff: SongDiff | ChannelDiff) -> str:
    """'onset 3  pitch 1', or '' when nothing differs."""
    counts = diff.counts()
    return "  ".join(f"{a.value} {counts[a]}" for a in Aspect if counts[a])


def song_diff_lines(diff: SongDiff, max_diffs: int, missing: str = "missing", extra: str = "extra") -> list[str]:
    """Every difference, `max_diffs` at most per channel; the last line the verdict.  `missing` /
    `extra`: what a channel only the expected / only the compared song has is called."""
    lines = [f"  {what:<14} {want} -> {got}" for what, want, got in diff.song]
    lines += [f"  {name:<5} {missing}" for name in diff.missing_channels]
    lines += [f"  {name:<5} {extra}" for name in diff.extra_channels]

    for ch in diff.channels:
        lines.append(f"  {ch.name:<5} {ch.notes:>4} notes   {diff_counts(ch) or 'same'}")
        found = [f"missing at {t}" for t in ch.missing] + [f"extra at {t}" for t in ch.extra]
        found += [f"{d.tick:>6}  {d.aspect.value:<10} {_show(ch.name, d)}" + (f"   x{n}, last at {last}" if n > 1 else "")
                  for d, n, last in _grouped(ch.changed)]
        lines += [f"        {line}" for line in found[:max_diffs]]
        if len(found) > max_diffs:
            lines.append(f"        ... {len(found) - max_diffs} more")

    lines.append("  same" if diff.ok else f"  {diff_counts(diff)}")
    return lines


def _pitch(channel: str, word: object) -> str:
    """A frequency word with the note it sounds: 0x2C3B (A4)."""
    if not isinstance(word, int):
        return str(word)
    if channel.startswith("FM"):
        hz = fm_frequency_hz(word & ((1 << _FNUM_BITS) - 1), word >> _FNUM_BITS, MD_FM_CLOCK)
    else:
        hz = psg_frequency_hz(max(word, 1), MD_PSG_CLOCK)
    return f"{word:#06x} ({pitch_name(hz)})"


def _voice_change(want: object, got: object) -> str:
    """An FM voice difference as the registers that differ: B0 and (register, byte) pairs."""
    if not (isinstance(want, tuple) and isinstance(got, tuple)):
        return f"{want} -> {got}"
    (b0_want, regs_want), (b0_got, regs_got) = want, got
    parts = [f"B0 {b0_want:#04x}->{b0_got:#04x}"] if b0_want != b0_got else []
    a, b = dict(regs_want), dict(regs_got)
    parts += [f"{r:#04x} {a.get(r)}->{b.get(r)}" for r in sorted(a.keys() | b.keys()) if a.get(r) != b.get(r)]
    return ", ".join(parts) or "same"


def _show(channel: str, d: NoteDiff) -> str:
    if d.aspect in (Aspect.NOTE, Aspect.PITCH):
        return f"{_pitch(channel, d.expected)} -> {_pitch(channel, d.got)}"
    if d.aspect is Aspect.VOICE and channel.startswith("FM"):
        return _voice_change(d.expected, d.got)
    return f"{d.expected} -> {d.got}"


def _grouped(changed: list[NoteDiff]) -> list[tuple[NoteDiff, int, int]]:
    """One difference made again and again (a wrong voice on every note) as (its first, how often,
    the last tick), in order of first appearance."""
    groups: dict[tuple, list] = {}
    for d in changed:
        group = groups.setdefault((d.aspect, repr(d.expected), repr(d.got)), [d, 0, d.tick])
        group[1] += 1
        group[2] = d.tick
    return [(d, n, last) for d, n, last in groups.values()]
