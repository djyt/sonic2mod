"""What each MOD sample slot is called (settings.yaml samples.names: source): what plays it and what
it was made from, in the 22 characters a sample name has.  A synthesised sample used to carry its
sample_list file name (`ghz_v00.raw`: no such file, four characters spent on `.raw`), a composite
`merge ` and its group (cut off past 21: `merge FM5+FM3+FM4+PSG`), twins the same name twice.

    F1/3/4/5 $05 C4-B5    an FM voice's range, the channels that play it
    F5 $04 C6-B7 dt+2     a detune variant (core/plan/detune.py)
    P1/2 fTone01          a PSG tone (its smpsPSGvoice label)
    P3 noise E7 fTone04   a PSG noise form and its envelope
    D dKick B-2           a DAC sample and the note it plays at (FT2's octaves)
    F5+3+4+P1 D-3 [1-4]   a composite: primary first, the note it plays most (twins numbered: #2),
                          its patterns
    bank D+F2+P3 9 hits   a sample bank (core/merge/banks.py)

Channels are F1-F5 (FM), P1-P3 (PSG) and D (DAC), the letter left out while it repeats.  A stated
`name:` (a voice_map / psg entry, a merge group) replaces the generated one; a sample loaded from
disk that is none of these keeps its file name, `.raw` off.  Parts are dropped from the end of a
name that would not fit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence

from ..config import ConversionConfig
from ..merge import MergePlan
from ..mod import MOD_NOTE_MAP, PERIOD_TABLE, ModFile
from ..mod.sample_audit import note_name
from ..plan import fm_catalogue, psg_catalogue
from ..smps import SmpsSong
from ..smps import semitone_to_note_name as _semitone_to_name

NAME_CHARS = 22


def channel_label(sources, sep: str) -> str:
    """F5+3+4+P1: each source as its kind's letter and number, the letter left out while it repeats."""
    out, prev = [], None
    for s in sources:
        kind, num = ("D", "") if s == "DAC" else (s[0], s[-1])
        out.append(num if kind == prev and num else kind + num)
        prev = kind
    return sep.join(out)


def _sorted_sources(sources) -> list[str]:
    order = {"DAC": 0, "F": 1, "P": 2}
    return sorted(sources, key=lambda s: (order.get(s if s == "DAC" else s[0], 3), s))


def _fit(parts: Sequence[str | None]) -> str:
    """The parts joined, the last dropped while the name is longer than a sample name holds; the
    first two (what it is) are cut short rather than dropped."""
    kept = [p for p in parts if p]
    while len(kept) > 2 and len(" ".join(kept)) > NAME_CHARS:
        kept.pop()
    return " ".join(kept)[:NAME_CHARS]


def _note(semitone: int | None) -> str:
    return _semitone_to_name(semitone).replace("s", "#") if semitone is not None else ""


def _range(low: int | None, high: int | None) -> str:
    if low is None and high is None:
        return ""
    if low == high or high is None:
        return _note(low)
    return f"{_note(low)}-{_note(high)}"


def _played_notes(mod: ModFile) -> dict[int, str]:
    """{slot: the note it is played at most often, FT2's spelling}."""
    counts: dict[int, Counter] = defaultdict(Counter)
    for pat in mod.patterns:
        data = pat.get_bytes()
        for i in range(0, len(data), 4):
            ins = (data[i] & 0xF0) | (data[i + 2] >> 4)
            period = ((data[i] & 0x0F) << 8) | data[i + 1]
            if ins and period:
                counts[ins][period] += 1
    return {ins: note_name(c.most_common(1)[0][0]) for ins, c in counts.items()}


def sample_names(mod: ModFile, song: SmpsSong, config: ConversionConfig, played: dict[int, set[str]],
                 merge: MergePlan | None, noise_envelopes: dict | None = None) -> dict[int, str]:
    """{slot: name} for every slot that holds a sample, as the module docstring describes."""
    notes = _played_notes(mod)
    names: dict[int, str] = {}

    def chans(inst: int, fallback: str = "") -> str:
        srcs = played.get(inst)
        return channel_label(_sorted_sources(srcs), "/") if srcs else fallback

    # The DAC samples off disk, at the note their slot plays them at
    dac: dict[int, list] = defaultdict(list)
    for d in config.dac_samples:
        dac[d.mod_instrument].append(d)
    for inst, ds in dac.items():
        first = ds[0]
        label = first.name + (f"+{len(ds) - 1}" if len(ds) > 1 else "")
        note = MOD_NOTE_MAP.get(first.mod_note)
        names[inst] = _fit(["D", label, note_name(PERIOD_TABLE[note.value]) if note is not None else ""])

    for spec in fm_catalogue(song, config).instruments.values():
        e, lay = spec.entry, spec.layers[0]
        if getattr(e, "name", None):
            names[spec.inst] = _fit([e.name])
            continue
        fallback = channel_label([spec.source_label], "/") if spec.source_label.startswith(("FM", "PSG")) else ""
        dt = f"dt{lay.fnum_offset:+d}" if lay.fnum_offset else ""
        names[spec.inst] = _fit([chans(spec.inst, fallback), f"${lay.voice_idx:02X}", _range(e.low, e.high), dt])

    for p in psg_catalogue(config, noise_envelopes).values():
        e = p.entry
        if getattr(e, "name", None):
            names[p.inst] = _fit([e.name])
        elif e.type == "tone":
            names[p.inst] = _fit([chans(p.inst), p.source.replace("_", ""), _range(e.low, e.high)])
        else:
            env = e.envelope if isinstance(e.envelope, str) else ""
            names[p.inst] = _fit([chans(p.inst, "P3"), "noise", p.source.lstrip("$"), env.replace("_", "")])

    if merge is not None:
        comps: dict[int, list[str]] = {}
        for c in merge.composites.values():
            if c.banked:
                continue
            g = c.group
            label = getattr(g, "name", None) or channel_label([g.primary, *g.followers], "+")
            comps[c.inst] = [label, notes.get(c.inst, ""), f"[{g.where.strip(' []')}]" if g.where else ""]
        # Twins (two chord shapes made at one note) are numbered, ahead of the patterns that may not fit
        seen = Counter(_fit(p) for p in comps.values())
        nth: Counter = Counter()
        for inst in sorted(comps):
            parts = comps[inst]
            plain = _fit(parts)
            if seen[plain] > 1:
                nth[plain] += 1
                parts = [*parts[:2], f"#{nth[plain]}", *parts[2:]]
            names[inst] = _fit(parts)
        for b in merge.banks:
            srcs = {s for m in b.members for s in (m.group.primary, *m.group.followers)}
            names[b.slot] = _fit(["bank", channel_label(_sorted_sources(srcs), "+"), f"{len(b.members)} hits"])

    # Anything else off disk keeps its file name
    for e in config.sample_list or []:
        inst = e[0]
        if inst not in names and len(e) > 1 and isinstance(e[1], str):
            names[inst] = _fit([e[1].removesuffix(".raw")])
    return {i: n for i, n in names.items() if 0 < i <= len(mod.samples) and mod.samples[i - 1].length}
