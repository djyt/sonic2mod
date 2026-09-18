#!/usr/bin/env python3
"""Convert a song config's ranges from source bytes to the pitches the chip plays (range_space: chip).

Why: a voice_map / psg_voice_map entry keyed on source bytes with a `root` anchor assumes one
transposition.  When channels with different pitch_offsets share the entry (Stage Clear's PSG1/PSG2,
Ending's), or a channel changes key with smpsChangeTransposition (Continue, Star Light), the same
byte has to reach different MOD notes and the anchor puts some of them an octave or a few
semitones out.  In chip space every entry says which real pitches it covers, so both cases work.

What it does, per entry (source low..high, root R, synth_root S):
  for every channel that plays the voice/label, the chip pitches of its notes whose source byte
  falls in low..high are collected; each distinct range becomes an entry
      low = min chip pitch, high = max chip pitch, root = R + (low - S), synth_root = low
  (the sample is rendered from the entry's own root/synth_root pair, so the two move together and
  the tuning S - R that every note hears is unchanged; chip pitch p sounds from MOD note
  R + (p - S) whichever channel it comes from).  Ranges from different channels are merged when
  they touch, since they share the formula.

The voice_map, psg_voice_map and channel_instrument_map sections are rewritten (their comments
are replaced by "# was low X" notes); everything else in the file is kept.  Run the pitch audit
afterwards:

    python tools/config_to_chip_space.py configs/12_ending_theme.yaml
    python tools/vgm_pitch_audit.py configs/12_ending_theme.yaml "reference/vgz/12 - Ending Theme.vgz"
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig
from core.driver_state import chip_pitch
from core.smps_parser import SmpsParser
from core.tables import parse_smps_note, parse_synth_note, semitone_to_note_name, synth_note_name

mod_name = synth_note_name   # YAML config note name for a semitone


def chip_notes(cfg: ConversionConfig):
    """{(kind, key, channel): [(source semitone, chip pitch), ...]} for FM voices and PSG labels.

    kind "fm": key = voice index; kind "psg": key = psg_voice_map label (as written).  The PSG
    label in force is the header voice ("$00" when the header says 0) until smpsPSGvoice; a
    channel in noise mode contributes nothing.
    """
    # The converter's view of the song: loops extended (a smpsChangeTransposition inside the loop
    # body accumulates on every replay - Continue's later notes sit lower than the first pass).
    from core.config import PsgSynthesisSettings, SynthesisSettings
    from core.smps2mod import SmpsToModConverter
    song = SmpsParser().parse_file(cfg.input_file)
    fm_off, psg_off = SynthesisSettings(), PsgSynthesisSettings()
    fm_off.enabled = psg_off.enabled = False
    conv = SmpsToModConverter(song, cfg, synth=fm_off, psg_synth=psg_off)
    conv._apply_global_tempo_div()
    conv._extend_looping_channels()
    out: dict[tuple, list[tuple[int, int]]] = defaultdict(list)     # every (source, chip) pair
    labels = {str(k) for k in cfg.psg_voice_map}
    for ch in song.channels:
        kind = ch.header.channel_type
        if kind == "DAC":
            continue
        name = ch.header.label.rsplit("_", 1)[-1]
        tr = ch.header.pitch_offset
        cur = None
        noise = False
        if kind == "PSG":
            cur = ch.header.psg_voice_label or "$00"
        for ev in ch.events:
            if ev.is_effect:
                k = ev.effect.effect_type
                if k == "smpsSetvoice":
                    cur = ev.effect.params[0]
                elif k == "smpsChangeTransposition":
                    tr += ev.effect.params[0]
                elif k == "smpsPSGvoice" and not noise and str(ev.effect.params[0]) in labels:
                    cur = ev.effect.params[0]          # an unknown label leaves the entry in force (as the converter does)
                elif k == "smpsPSGform":
                    noise = True
            elif ev.is_note and not ev.note.is_rest and not noise and cur is not None:
                src = ev.note.note_value - 0x81
                pitch = chip_pitch(src, tr, kind == "PSG")
                if kind == "FM":
                    out[("fm", cur, name)].append((src, pitch))
                elif str(cur) in labels:
                    out[("psg", str(cur), name)].append((src, pitch))
    return out


def convert_entries(entries: list[dict], notes_by_channel: dict[str, list[tuple[int, int]]], only_channel=None):
    """New entries in chip space for one voice / label.  `entries` are the YAML dicts.

    Every note the voice plays is attached to the entry whose source range covers it, or to the
    nearest entry when none does (a note the old config never covered fell to the transpose path
    and was wrong anyway).  Each entry's chip range is then the min..max of its notes per channel,
    touching ranges merged.  root is R + (low - S) so the tuning S - R is kept, clamped so the
    whole range stays inside MOD C1..B3.
    """
    parsed = [(parse_smps_note(str(e["low"])), parse_smps_note(str(e["high"])),
               parse_synth_note(str(e["root"])), parse_synth_note(str(e["synth_root"])), e) for e in entries]
    assigned: list[dict[str, list[int]]] = [defaultdict(list) for _ in parsed]
    for chan, m in notes_by_channel.items():
        if only_channel and chan != only_channel:
            continue
        for src, p in m:
            hit = [i for i, (lo, hi, *_r) in enumerate(parsed) if lo <= src <= hi]
            if not hit:
                hit = [min(range(len(parsed)), key=lambda i: min(abs(src - parsed[i][0]), abs(src - parsed[i][1])))]
            assigned[hit[0]][chan].append(p)
    new = []
    for (lo, hi, r, s, e), per_chan in zip(parsed, assigned, strict=True):
        ranges = sorted((min(ps), max(ps), chan) for chan, ps in per_chan.items())
        merged: list[tuple[int, int, str]] = []
        for a, b, chan in ranges:
            if merged and a <= merged[-1][1] + 1:
                pa, pb, pc = merged[-1]
                merged[-1] = (pa, max(pb, b), f"{pc}, {chan}")
            else:
                merged.append((a, b, chan))
        if not merged:                      # never played: keep, translated by nothing
            merged = [(lo, hi, "unused")]
        for a, b, chans in merged:
            if b - a > 35:
                print(f"  WARNING: {e['mod_instrument']}: chip range {semitone_to_note_name(a)}-{semitone_to_note_name(b)} "
                      f"spans more than three octaves; the top will clamp", file=sys.stderr)
            # The sample is rendered from the entry's own (root, synth_root) pair, so synth_root
            # becomes the chip pitch at `low`; root keeps the tuning R + (low - S) where it can,
            # clamped so low..high fits MOD C1..B3 (SMPS semitones 12..47).
            root = r + (a - s)
            fit = min(max(root, 12), max(12, 47 - (b - a)))
            if fit != root:
                # Moving root alone would retune the sample, which other entries of the same
                # instrument still expect; those notes need an instrument of their own.
                print(f"  WARNING: instrument {e['mod_instrument']}: chip range {semitone_to_note_name(a)}-"
                      f"{semitone_to_note_name(b)} does not fit MOD C1..B3 at the sample's tuning (root would be "
                      f"{mod_name(root)}); clamped to {mod_name(fit)} - give these notes a new mod_instrument "
                      f"(same voice, synth_root {semitone_to_note_name(a)}, root {mod_name(fit)})", file=sys.stderr)
                root = fit
            new.append({"low": a, "high": b, "root": root, "synth_root": a,
                        "mod_instrument": e["mod_instrument"], "was": f"{e['low']}-{e['high']} (source bytes) for {chans}",
                        "extra": {k: v for k, v in e.items() if k not in ("low", "high", "root", "synth_root", "mod_instrument")}})
    return new


def render(new: list[dict], indent: str) -> list[str]:
    out = []
    for e in new:
        out.append(f"{indent}- low:  {semitone_to_note_name(e['low'])}   # was {e['was']}")
        out.append(f"{indent}  high: {semitone_to_note_name(e['high'])}")
        out.append(f"{indent}  mod_instrument: {e['mod_instrument']}")
        out.append(f"{indent}  root: {mod_name(e['root'])}")
        out.append(f"{indent}  synth_root: {semitone_to_note_name(e['synth_root'])}")
        for k, v in e["extra"].items():
            out.append(f"{indent}  {k}: {v}")
    return out


def main() -> None:
    path = Path(sys.argv[1])
    cfg = ConversionConfig.from_yaml(str(path))
    if cfg.range_space == "chip":
        sys.exit(f"{path} is already in chip space")
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    notes = chip_notes(cfg)

    sections: dict[str, list[str]] = {}
    if "voice_map" in data:
        lines = ["voice_map:"]
        for v, raw in data["voice_map"].items():
            ents = raw if isinstance(raw, list) else [raw]
            per_chan = {c: m for (kind, key, c), m in notes.items() if kind == "fm" and key == int(v)}
            lines.append(f"  {v}:")
            lines += render(convert_entries(ents, per_chan), "    ")
        sections["voice_map"] = lines
    if "psg_voice_map" in data:
        lines = ["psg_voice_map:"]
        for label, raw in data["psg_voice_map"].items():
            ents = raw if isinstance(raw, list) else [raw]
            if ents and ents[0].get("type", "tone") != "tone":
                lines.append(f'  {label if not str(label).startswith("$") else chr(34) + str(label) + chr(34)}:')
                for e in ents:
                    for k, v in e.items():
                        lines.append(f"    {k}: {v}")
                continue
            per_chan = {c: m for (kind, key, c), m in notes.items() if kind == "psg" and key == str(label)}
            key = f'"{label}"' if str(label).startswith("$") else str(label)
            lines.append(f"  {key}:")
            if all("low" in e for e in ents):
                lines += render(convert_entries(ents, per_chan), "    ")
            else:                                   # rootless / range-less entries stay as they are
                for e in ents:
                    for k, v in e.items():
                        lines.append(f"    {k}: {v}")
        sections["psg_voice_map"] = lines
    if "channel_instrument_map" in data:
        lines = ["channel_instrument_map:"]
        for chan, per_voice in data["channel_instrument_map"].items():
            lines.append(f"  {chan}:")
            for v, raw in per_voice.items():
                ents = raw if isinstance(raw, list) else [raw]
                per_chan = {c: m for (kind, key, c), m in notes.items() if kind == "fm" and key == int(v)}
                lines.append(f"    {v}:")
                lines += render(convert_entries(ents, per_chan, only_channel=chan), "    ")
        sections["channel_instrument_map"] = lines

    # Replace each section's text block (from its top-level key to the next top-level key).
    top = re.compile(r"^([A-Za-z_]+):", re.MULTILINE)
    keys = [(m.start(), m.group(1)) for m in top.finditer(text)]
    out = text
    for start, name in reversed(keys):
        if name not in sections:
            continue
        nxt = min([s for s, _ in keys if s > start], default=len(text))
        # keep any comment block that directly precedes the next key
        block = text[start:nxt]
        tail = ""
        m = re.search(r"(\n(#[^\n]*\n)+)$", block)
        if m:
            tail = m.group(1)
        out = out[:start] + "\n".join(sections[name]) + "\n" + tail + out[nxt:]
    out = out.replace("region: ntsc\n", "region: ntsc\nrange_space: chip          # ranges are the pitches the chip plays (tools/config_to_chip_space.py)\n", 1)
    path.write_text(out, encoding="utf-8", newline="")
    print(f"{path}: converted to range_space: chip")


if __name__ == "__main__":
    main()
