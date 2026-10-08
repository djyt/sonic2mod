"""Per-instrument level error of a MOD against its VGZ, and the sample_list volumes that zero it."""

from __future__ import annotations

import re
import statistics
from pathlib import Path

from ..audio import db_to_gain, gain_to_db
from ..config import SAMPLE_FILE, SAMPLE_FINETUNE, SAMPLE_SLOT, SAMPLE_VOLUME
from ..mod import ModImage, edx_delay, timed_pass
from .signal import db, rms, seg_at


def mod_note_events(mod: ModImage, speed: int) -> tuple[dict[int, list[tuple]], dict[int, tuple[str, int]]]:
    """({channel: [(time s, instrument, Cxx value or None)]}, {instrument: (name, volume)}) over the
    pass core.mod.timed_pass times, so its notes and the pitch timeline share one clock."""
    samples = {i: (s.name, s.volume) for i, s in enumerate(mod.samples, 1) if s.length}
    events: dict[int, list[tuple]] = {c: [] for c in range(mod.channels)}
    rows, _end = timed_pass(mod, speed)
    for r in rows:
        for c, (period, ins, eff, par) in enumerate(r.cells):
            if period and ins:
                events[c].append((r.start + edx_delay(eff, par, r.bpm), ins, par if eff == 0xC else None))
    return events, samples


_LEVEL_SPAN = 0.6            # seconds of a note that count towards its level
_LEVEL_MIN_NOTES = 4         # fewer plain notes than this and no volume is suggested
_LEVEL_SCALE_MIN_NOTES = 12  # fewer notes than this and the instrument cannot drive the common scale-down
LEVEL_MAX_SPREAD = 3.0      # dB between channels sharing an instrument before it is "not a volume problem"
_LEVEL_DAC_SLACK = 2.0       # dB the DAC must be BELOW the song's median before everything is balanced to it
LEVEL_MAX_ERR = 18.0        # dB; beyond this something other than the volume is wrong (silent / wrong instrument)


def instrument_levels(note_times: dict[str, list[float]], mod_chan: dict[str, int], events: dict[int, list[tuple]],
                      samples: dict[int, tuple[str, int]], mod_end: float, vgm_st: dict, mod_st: dict,
                      offset: float) -> dict:
    """Level error MOD - VGM per (chip channel, MOD instrument, Cxx), and per instrument.

    Errors are relative to an anchor so the unknown gain between the two renders drops out.
    The anchor is the median over every plain (no Cxx) synthesised note, so that only RELATIVE
    imbalance is reported — unless the DAC is QUIETER than that median by _LEVEL_DAC_SLACK dB or
    more.  DAC samples sit at volume 64 and cannot be turned up, so then everything else has to
    come down to meet them.  A DAC that is too LOUD is simply turned down (it gets a suggestion
    like any instrument), and a gap under the slack is within what short DAC hits can be measured
    to — chasing it would rewrite every volume for nothing.
    Only notes without a Cxx say what the instrument's own volume should be — unless it has none
    long enough to measure, when its Cxx notes do (`from_cxx`).
    """
    groups: dict[tuple, list[float]] = {}
    for ch, times in note_times.items():
        evs = events.get(mod_chan[ch], [])
        short = ch in ("DAC", "NOISE")
        for i, t in enumerate(times):
            if t > mod_end - 0.3:
                break
            dur = min((times[i + 1] - t) if i + 1 < len(times) else _LEVEL_SPAN, 0.15 if ch == "DAC" else _LEVEL_SPAN)
            if dur < (0.04 if short else 0.09):
                continue
            lv = db(rms(seg_at(vgm_st[ch], t, dur)))
            lm = db(rms(seg_at(mod_st[ch], t + offset, dur)))
            if lv < -55 or lm < -75:
                continue
            hit = None
            for e in evs:
                if e[0] > t + offset + 0.03:
                    break
                hit = e
            if hit is not None:
                groups.setdefault((ch, hit[1], hit[2]), []).append(lm - lv)

    def med(keys) -> float | None:
        xs = [x for k in keys for x in groups[k]]
        return statistics.median(xs) if xs else None

    dac = med(k for k in groups if k[0] == "DAC")
    n_dac = sum(len(groups[k]) for k in groups if k[0] == "DAC")
    song = med(k for k in groups if k[0] != "DAC" and k[2] is None)
    if song is None:
        song = med(k for k in groups if k[0] != "DAC")
    if song is None:
        return {"anchor": None, "groups": [], "instruments": []}
    dac_vs_song = (dac - song) if dac is not None and n_dac >= 8 else None
    anchor: float
    if dac is not None and dac_vs_song is not None and dac_vs_song <= -_LEVEL_DAC_SLACK:
        anchor_name, anchor = f"the DAC (which is {dac_vs_song:+.1f} dB against the rest of the song)", dac
    else:
        anchor = song
        anchor_name = "the song's median note" + (
            f" (DAC {dac_vs_song:+.1f} dB against it)" if dac_vs_song is not None else "")

    out_groups = [{"channel": ch, "instrument": ins, "cxx": cxx, "notes": len(xs),
                   "err_db": statistics.median(xs) - anchor}
                  for (ch, ins, cxx), xs in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2] or -1))]
    instruments = []
    for ins in sorted({g["instrument"] for g in out_groups}):
        plain = [g for g in out_groups if g["instrument"] == ins and g["cxx"] is None]
        from_cxx = not plain
        if from_cxx:
            # Its plain notes too short to measure (Invincibility FM5: the baked level is the fast run's,
            # every long note a Cxx).  A Cxx is the volume scaled by the note's level difference, so its
            # error is the volume's; one at 64 may be clamped and says nothing
            plain = [g for g in out_groups if g["instrument"] == ins and g["cxx"] is not None and g["cxx"] < 64]
            if not plain:
                continue
        notes = sum(g["notes"] for g in plain)
        err = statistics.median(x for g in plain for x in groups[(g["channel"], ins, g["cxx"])]) - anchor
        spread = max(g["err_db"] for g in plain) - min(g["err_db"] for g in plain)
        name, vol = samples.get(ins, ("?", 64))
        ok = notes >= _LEVEL_MIN_NOTES and spread <= LEVEL_MAX_SPREAD and abs(err) <= LEVEL_MAX_ERR
        instruments.append({
            "instrument": ins, "name": name, "volume": vol, "notes": notes, "err_db": err, "spread_db": spread,
            "channels": sorted({g["channel"] for g in plain}), "from_cxx": from_cxx,
            "wanted": vol * db_to_gain(-err) if ok else None,
        })
    scale = suggest_volumes(instruments)
    return {"anchor": anchor_name, "anchor_db": anchor, "groups": out_groups, "instruments": instruments,
            "scaled_db": gain_to_db(scale)}


def suggest_volumes(instruments: list[dict]) -> float:
    """Fill in `suggested` from `wanted`; returns the common scale applied (1.0 = none).

    64 is the ceiling.  An instrument that wants a little more than that (within the slack that a
    level can be measured to — typically a DAC sample already at 64 reading a hair quiet) is
    simply clamped.  Only when one wants substantially more are ALL suggestions brought down
    together, so that the balance between them survives.
    """
    # Only instruments with a fair number of notes may pull everything down: Credits' fm_v0e
    # (4 notes, read -9.9 dB) would otherwise have cost every sample 7.9 dB and left the PSG
    # tones at volume 1-4.  A few-note instrument that wants more than 64 is clamped instead.
    wanted = [it["wanted"] for it in instruments
              if it.get("wanted") is not None and it.get("notes", 0) >= _LEVEL_SCALE_MIN_NOTES]
    ceiling = 64.0 * db_to_gain(_LEVEL_DAC_SLACK)
    scale = min(1.0, ceiling / max(wanted)) if wanted else 1.0
    for it in instruments:
        it["suggested"] = None if it.get("wanted") is None else max(1, min(64, round(it["wanted"] * scale)))
    return scale


def write_volumes(config_path: Path, instruments: list[dict], min_db: float = 1.0,
                  settings_path: str | None = None) -> list[str]:
    """Set sample_list volumes to the suggested values; returns a line per change.  A minimal
    config (derived under `settings_path`, as the MOD was) gains rows for derived samples it lacks."""
    text = config_path.read_text(encoding="utf-8")
    derived = _derived_rows(config_path, settings_path)

    # A minimal config's rows follow their file (core.plan.derive): each numbered as its slot is now
    text = _renumber_rows(text, {row[SAMPLE_FILE]: row[SAMPLE_SLOT] for row in derived.values()})

    added: list[str] = []
    changes = []
    for it in instruments:
        new = it["suggested"]
        if new is None or new == it["volume"] or abs(gain_to_db(new / it["volume"])) < min_db:
            continue
        inst, row = it["instrument"], derived.get(it["instrument"])
        change = f"  instrument {inst:>2} ({it['name']}): {it['volume']} -> {new}  ({it['err_db']:+.1f} dB)"
        note = f"VGZ: {it['err_db']:+.1f} dB at {it['volume']}"

        # The instrument's row: by slot, and by file in a minimal config
        m = _row_pattern(str(inst), re.escape(row[SAMPLE_FILE]) if row else _ANY_FILE).search(text)

        # A derived sample with no row: one added
        if not m and row is not None and row[SAMPLE_VOLUME] == it["volume"]:
            added.append(f'  - [{inst}, "{row[SAMPLE_FILE]}", {new}, {row[SAMPLE_FINETUNE]}]   # {note}')
            changes.append(change)
            continue
        if not m or int(m["volume"]) != it["volume"]:
            changes.append(f"  !! instrument {inst}: no sample_list line with volume {it['volume']} — not changed")
            continue

        # The volume replaced, the comment's earlier VGZ note with it
        tail = re.sub(r"\s*[;#]?\s*VGZ: [^;]*", "", m["tail"]).rstrip()
        tail = f"{tail}; {note}" if tail.strip().startswith("#") and tail.strip() != "#" else f" # {note}"
        text = text[:m.start("volume")] + f"{new:>{len(m['volume'])}}{m['end']}{tail}" + text[m.end():]
        changes.append(change)

    if added:
        text = _add_rows(text, added)
    if any(not c.startswith("  !!") for c in changes):
        config_path.write_text(text, encoding="utf-8", newline="")
    return changes


_ANY_SLOT = r"\d+"
_ANY_FILE = r'[^"]*'


def _row_pattern(slot: str = _ANY_SLOT, file: str = _ANY_FILE) -> re.Pattern:
    """A sample_list row of the config's text: `  - [5, "fm_v01_F2.raw", 45, 0]   # comment`."""
    return re.compile(rf'^(?P<head>\s*-\s*\[\s*)(?P<slot>{slot})\s*,\s*"(?P<file>{file})"\s*,\s*'
                      rf'(?P<volume>\d+)(?P<end>(?:\s*,\s*-?\d+)?\s*\])(?P<tail>[^\r\n]*)', re.M)


def _renumber_rows(text: str, slot_of: dict[str, int]) -> str:
    """Each row naming a file in `slot_of` given that file's slot."""
    def renumber(m: re.Match) -> str:
        return m["head"] + str(slot_of.get(m["file"], m["slot"])) + m[0][m.end("slot") - m.start():]
    return _row_pattern().sub(renumber, text)


def _derived_rows(config_path: Path, settings_path: str | None = None) -> dict[int, list]:
    """A minimal config's sample_list as derived (by instrument); nothing for a full config."""
    from ..config import ConversionConfig
    from ..plan import load_config

    if not ConversionConfig.from_yaml(str(config_path)).is_minimal:
        return {}
    return {row[SAMPLE_SLOT]: row for row in load_config(config_path, settings_path).sample_list or []}


def _add_rows(text: str, rows: list[str]) -> str:
    """Rows put under the config's sample_list: (made at its end when it has none)."""
    m = re.search(r"^sample_list:[^\n]*\n", text, re.M)
    if m is None:
        return text.rstrip("\n") + "\n\nsample_list:   # volumes set from the VGZ; the rest is derived\n" + "\n".join(rows) + "\n"
    return text[:m.end()] + "\n".join(rows) + "\n" + text[m.end():]
