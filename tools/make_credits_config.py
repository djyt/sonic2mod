"""Build configs/13_credits.yaml in chip-pitch space (range_space: chip), 31 instruments.

Usage::

    python analyze.py "sonic_1/music/Mus91 - Credits.asm" --write output/credits_skeleton.yaml
    python tools/make_credits_config.py output/credits_skeleton.yaml

Re-running keeps the volumes (and their VGZ / by-hand comments) of the existing config.

Every FM voice's notes are collected as the real pitch the chip plays (byte + pitch_offset +
smpsChangeTransposition), split into windows of at most three octaves, and each window becomes a
voice_map entry synthesised at its lowest note (`root: C1` there).  PSG tones likewise (real pitch
= table index + 3 octaves), packed by range into as few instruments as possible.  The FM budget is
31 - DAC 3 - noise 1 - PSG tone instruments; voices are merged pairwise, most similar first
(operator distance, same algorithm), where the combined range still fits three octaves.  A merged
voice keeps its own entries, pointing at the shared instrument with its root shifted so its notes
sound right.  Volumes are the analyze.py skeleton's estimates (set from the VGZ afterwards).
"""
import re
import sys
from collections import Counter, defaultdict

import yaml

sys.path.insert(0, ".")
from core.driver_tables import psg_index_semitone
from core.smps_parser import SmpsParser
from core.tables import semitone_to_note_name

SKEL = sys.argv[1] if len(sys.argv) > 1 else "output/credits_skeleton.yaml"
OUT = "configs/13_credits.yaml"
MOD_LO, MOD_SPAN = 12, 35          # MOD C1 in SMPS semitones; C1..B3
_MOD_NAMES = ["C", "Cs", "D", "Ds", "E", "F", "Fs", "G", "Gs", "A", "As", "B"]

with open(SKEL, encoding="utf-8") as fh:
    skel = yaml.safe_load(fh)
song = SmpsParser().parse_file("sonic_1/music/Mus91 - Credits.asm")
voices = {v.index: v for v in song.voices}


def nums(s):
    return [int(x.strip().lstrip("$"), 16) for x in s.split(",")]


def vec(v):
    out = []
    for k, w in (("smpsVcCoarseFreq", 6), ("smpsVcTotalLevel", 1), ("smpsVcDecayRate1", 1), ("smpsVcDecayLevel", 2),
                 ("smpsVcAttackRate", 1), ("smpsVcReleaseRate", 1), ("smpsVcDetune", 2)):
        out += [x * w for x in nums(v.params[k])]
    return out


def dist(a, b):
    return sum(abs(x - y) for x, y in zip(vec(a), vec(b), strict=True)) + (0 if a.feedback == b.feedback else 20)


def mod_name(semi):
    return f"{_MOD_NAMES[semi % 12]}{semi // 12}"


def smps_name(semi):
    return semitone_to_note_name(semi)


def windows(pitches):
    """Split sorted distinct pitches into runs spanning <= MOD_SPAN, cutting at the widest gaps."""
    ps = sorted(set(pitches))
    if ps[-1] - ps[0] <= MOD_SPAN:
        return [(ps[0], ps[-1])]
    out, start = [], 0
    while start < len(ps):
        end = start
        while end + 1 < len(ps) and ps[end + 1] - ps[start] <= MOD_SPAN:
            end += 1
        out.append((ps[start], ps[end]))
        start = end + 1
    return out


# ---- chip pitches per FM voice and per PSG envelope label
fm_notes: dict[int, Counter] = defaultdict(Counter)   # voice index -> chip pitch -> count
psg_notes: dict[str, Counter] = defaultdict(Counter)
for ch in song.channels:
    kind = ch.header.channel_type
    if kind == "DAC":
        continue
    tr, noise = ch.header.pitch_offset, False
    cur: int | str | None = None
    if kind == "PSG":
        cur = "$00"
    for ev in ch.events:
        if ev.is_effect:
            k = ev.effect.effect_type
            if k == "smpsSetvoice":
                cur = ev.effect.params[0]
            elif k == "smpsChangeTransposition":
                tr += ev.effect.params[0]
            elif k == "smpsPSGvoice" and not noise:
                cur = ev.effect.params[0]
            elif k == "smpsPSGform":
                cur, noise = None, True         # noise from here on (cfSetPSGNoise is permanent)
        elif ev.is_note and not ev.note.is_rest and cur is not None:
            p = ev.note.note_value - 0x81 + tr
            if kind == "FM" and isinstance(cur, int):
                fm_notes[cur][p] += 1
            else:                               # PSG tone (noise sections set cur = None above)
                psg_notes[str(cur)][psg_index_semitone(p)] += 1

# ---- PSG tone entries packed by range
psg_entries = []
for label, c in psg_notes.items():
    for lo, hi in windows(list(c)):
        psg_entries.append({"label": label, "lo": lo, "hi": hi, "n": sum(v for p, v in c.items() if lo <= p <= hi)})
psg_entries.sort(key=lambda e: e["lo"])
psg_packs: list[list[dict]] = []
for e in psg_entries:
    if psg_packs and max(x["hi"] for x in [*psg_packs[-1], e]) - min(x["lo"] for x in [*psg_packs[-1], e]) <= MOD_SPAN:
        psg_packs[-1].append(e)
    else:
        psg_packs.append([e])
FM_BUDGET = 31 - 3 - 1 - len(psg_packs)

# ---- FM windows -> groups (instruments)
entries: dict[int, list[dict]] = {}
for v, c in fm_notes.items():
    entries[v] = [{"voice": v, "lo": lo, "hi": hi, "n": sum(k for p, k in c.items() if lo <= p <= hi)}
                  for lo, hi in windows(list(c))]
groups: list[list[dict]] = [[e] for v in sorted(entries) for e in entries[v]]
merged: dict[int, int] = {}
merge_note = []


def span(es):
    return max(e["hi"] for e in es) - min(e["lo"] for e in es)


def plan(v, t):
    moves = []
    for g in [g for g in groups if g[0]["voice"] == v]:
        for e in g:
            opts = [h for h in groups if h[0]["voice"] == t and span([*h, e]) <= MOD_SPAN]
            if not opts:
                return None
            moves.append((e, min(opts, key=lambda h: span([*h, e])), g))
    return moves


while len(groups) > FM_BUDGET:
    best = None
    live = [v for v in entries if v not in merged]
    for v in live:
        for t in live:
            if t == v or voices[t].algorithm != voices[v].algorithm:
                continue
            moves = plan(v, t)
            if moves is None:
                continue
            cost = dist(voices[v], voices[t]) + 0.5 * sum(e["n"] for e in entries[v])
            if best is None or cost < best[0]:
                best = (cost, v, t, moves)
    if best is None:
        sys.exit("no feasible merge left")
    _, v, t, moves = best
    for e, h, g in moves:
        h.append(e)
        if g in groups:
            groups.remove(g)
    merged[v] = t
    merge_note.append(f"${v:02X} ({sum(e['n'] for e in entries[v])} notes, alg {voices[v].algorithm} "
                      f"fb {voices[v].feedback}) -> ${t:02X} (fb {voices[t].feedback}); distance {dist(voices[v], voices[t])}")

# ---- instruments: number, synthesis pitch (group's lowest note), per-entry roots
groups.sort(key=lambda g: (g[0]["voice"], g[0]["lo"]))
fm_inst = {}
for i, g in enumerate(groups, start=4):
    lo = min(e["lo"] for e in g)
    for e in g:
        e["inst"], e["root"], e["synth"] = i, MOD_LO + e["lo"] - lo, e["lo"]
    fm_inst[i] = g
fm_last = 3 + len(groups)
psg_inst_first = fm_last + 1
for i, pack in enumerate(psg_packs, start=psg_inst_first):
    lo = min(e["lo"] for e in pack)
    for e in pack:
        e["inst"], e["root"], e["synth"] = i, MOD_LO + e["lo"] - lo, e["lo"]
psg_noise = psg_inst_first + len(psg_packs)

# skeleton volume estimate per voice (its first instrument's sample_list row); an existing config's
# measured volumes (vgm_compare --write-volumes) win, by instrument number
skel_vol = {row[0]: row for row in skel["sample_list"]}
measured: dict[int, tuple[int, str]] = {}
try:
    with open(OUT, encoding="utf-8") as fh:
        for line in fh:
            t = line.strip()
            if t.startswith("- [") and "]" in t:
                body, _, comment = t[3:].partition("]")
                parts = [x.strip().strip('"') for x in body.split(",")]
                measured[int(parts[0])] = (int(parts[2]), comment.strip())
except FileNotFoundError:
    pass


def vol_of(inst, default):
    """(volume, trailing note) - the measured volume and only the measurement / by-hand part of
    its comment, so regenerating does not stack the generator's own comment up again."""
    if inst in measured:
        v, c = measured[inst]
        keep = re.findall(r"(VGZ: [^#;]+|-?\d+ dB by hand[^#;]*)", c)
        return v, ("   # " + "; ".join(k.strip() for k in keep) if keep else "")
    return default, ""
voice_vol = {}
for k, ents in skel["voice_map"].items():
    first = ents[0] if isinstance(ents, list) else ents
    row = skel_vol.get(first["mod_instrument"])
    voice_vol[int(k)] = (row[1], row[2]) if row else (f"fm_v{int(k):02x}.raw", 32)

lines = [
    "# -------------------------------------------------------------------------------------------------",
    "# CREDITS (Staff Roll) Conversion Script — generated by mk_credits.py (chip-pitch ranges)",
    "#",
    "# range_space: chip — voice_map / psg_voice_map low/high are the REAL pitches the chip plays, so the",
    "# medley's constant smpsChangeTransposition key changes (FM2: twenty of them) land on the right notes.",
    "# Each entry is synthesised at its lowest note and anchored at root (C1 for the instrument's lowest",
    "# entry); a voice spanning more than three octaves has one entry per window.",
    "#",
    "# 31 FM voices + 4 DAC samples + PSG would need more than a MOD's 31 instruments, so the most similar",
    "# voices of the same algorithm share one sample (the merged voice keeps its own entries):",
] + [f"#   {m}" for m in merge_note] + [
    "#",
    "# Tempo: smpsHeaderTempo $01,$33 (58.8 Hz ticks) with five smpsSetTempoMod steps (FM1) and a half-tempo",
    "# passage via smpsSetTempoDiv $02/$01 (DAC track).  Durations have GCD 1, so 2 ticks per row with EDx",
    "# for the odd ticks: BPM 147 at the header tempo, 100–140 in the steps.",
    "# -------------------------------------------------------------------------------------------------",
    "",
    "name: Credits",
    'input_file: "input/Mus91 - Credits.asm"',
    "output_file: output/13_credits.mod",
    'samples_dir: "samples/"',
    "",
    "auto_bpm: true",
    "target_speed: 2",
    "ticks_per_row: 2",
    "num_mod_channels: 10",
    "region: ntsc",
    "range_space: chip",
    "",
    "channels:",
]
for c in skel["channels"]:
    lines += [f"  - source: {c['source']}", f"    mod_channel: {c['mod_channel']}"]
lines += ["", "sample_list:", "  # --- percussion ---"]
for i in (1, 2, 3):
    r = skel_vol[i]
    v, c = vol_of(i, r[2])
    lines.append(f'  - [{i}, "{r[1]}", {v}, {r[3]}]{c}')
lines.append("  # --- FM voices (volumes: skeleton estimates; set from the VGZ with vgm_compare --write-volumes) ---")
for i, g in fm_inst.items():
    head = g[0]["voice"]
    fname, vol = voice_vol.get(head, (f"fm_v{head:02x}.raw", 32))
    who = ", ".join(sorted({f"${e['voice']:02X}" for e in g}, key=lambda s: int(s[1:], 16)))
    span_txt = f"{smps_name(min(e['lo'] for e in g))}-{smps_name(max(e['hi'] for e in g))}"
    v, c = vol_of(i, vol)
    lines.append(f'  - [{i}, "{fname}", {v}, 0]   # {who}  {span_txt}{c}')
lines.append("  # --- PSG ---")
for i, pack in enumerate(psg_packs, start=psg_inst_first):
    labels = sorted({e["label"] for e in pack})
    v, c = vol_of(i, 16)
    lines.append(f'  - [{i}, "psg_tone{i - psg_inst_first + 1}.raw", {v}, 0]   # {", ".join(labels)}  '
                 f"{smps_name(min(e['lo'] for e in pack))}-{smps_name(max(e['hi'] for e in pack))}{c}")
v, c = vol_of(psg_noise, 16)
lines += [f'  - [{psg_noise}, "psg_noise.raw", {v}, 0]{c}', "", "dac_samples:"]
for d in skel["dac_samples"]:
    lines += [f"  - name: {d['name']}", f"    mod_instrument: {d['mod_instrument']}", f"    mod_note: {d['mod_note']}"]
lines += ["", "voice_map:"]
for v in sorted(entries):
    vo = voices[v]
    tag = f"  # ${v:02X} — alg {vo.algorithm}, fb {vo.feedback}, {sum(e['n'] for e in entries[v])} notes"
    if v in merged:
        tag += f" — shares voice ${merged[v]:02X}'s sample"
    lines += [tag, f"  {v}:"]
    for e in entries[v]:
        lines += [f"    - low:  {smps_name(e['lo'])}", f"      high: {smps_name(e['hi'])}",
                  f"      mod_instrument: {e['inst']}", f"      root: {mod_name(e['root'])}",
                  f"      synth_root: {smps_name(e['synth'])}"]
lines += ["", "psg_map:"]
for k, e in skel["psg_map"].items():
    lines += [f"  {k}:", f"    mod_instrument: {psg_noise}", f"    root: {e['root']}", f"    noise_rate: {e['noise_rate']}",
              f"    envelope: {e['envelope']}", f"    base_volume: {e.get('base_volume', 0)}"]
lines += ["", "psg_voice_map:"]
by_label = defaultdict(list)
for e in psg_entries:
    by_label[e["label"]].append(e)
for label, es in by_label.items():
    key = f'"{label}"' if str(label).startswith("$") else label
    lines.append(f"  {key}:")
    for e in es:
        lines += [f"    - low:  {smps_name(e['lo'])}", f"      high: {smps_name(e['hi'])}",
                  f"      mod_instrument: {e['inst']}", f"      root: {mod_name(e['root'])}",
                  f"      synth_root: {smps_name(e['synth'])}"]
with open(OUT, "w", encoding="utf-8", newline="") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"wrote {OUT}: FM 4-{fm_last} ({len(groups)} instruments from {len(entries)} voices), "
      f"PSG tones {len(psg_packs)}, noise {psg_noise}")
print("\n".join(merge_note))
