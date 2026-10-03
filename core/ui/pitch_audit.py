"""The symbolic pitch audit's report (core.audit.audit_pitches), as vgm_pitch_audit.py and
vgm_compare.py print it."""

from __future__ import annotations

from collections import Counter


def verdict_text(v: dict) -> str:
    """One instrument_verdicts() entry as words; '' when the instrument has nothing wrong."""
    if v["semitones"]:
        n = abs(v["semitones"])
        size = f"{n // 12} octave{'s' if n // 12 > 1 else ''}" if n % 12 == 0 else f"{n} semitone{'s' if n > 1 else ''}"
        return (f"synth_root is {size} too {'high' if v['semitones'] > 0 else 'low'} "
                f"({v['uniform_notes']} of {v['notes']} notes)")
    if v["other"]:
        return "mixed: " + ", ".join(f"{c:+d} c x{k}" for c, k in v["other"][:4])
    return ""


def print_audit(res: dict, min_ms: float, listing: bool = False, indent: str = "") -> None:
    """The per-channel counts, the commonest wrong intervals, and the per-instrument verdicts."""
    for src, st in res["channels"].items():
        print(f"{indent}{src:<5} ok {st['ok']:>4}   wrong {st['wrong']:>3}   missing {st['missing']:>3}"
              f"   (+{st['short']} shorter than {min_ms:g} ms)")
        kinds = Counter((w["chip"], w["mod"], round(w["cents"] / 100) * 100, w["instrument"]) for w in st["wrong_notes"])
        for (a, b, c100, ins), k in kinds.most_common(8):
            print(f"{indent}        chip {a:<4} MOD {b:<4} ({c100:+5d} c)  inst {ins:<3} x{k}")
        if listing:
            rows = [(w["t_s"], f"chip {w['chip']:<4}  MOD {w['mod']:<4} {w['cents']:+6.0f} c  inst {w['instrument']} "
                               f"(placed {w['placed_s']:.2f} s)") for w in st["wrong_notes"]]
            rows += [(m["t_s"], f"chip {m['chip']:<4}  no MOD note yet") for m in st["missing_notes"]]
            for t, text in sorted(rows):
                print(f"{indent}      {t:7.2f} s  {text}")

    # An instrument whose notes are all out by the same interval is synthesised at the wrong pitch:
    # its synth_root is off by that interval.  Anything else is a note problem.
    if any(verdict_text(v) for v in res["instruments"]):
        print()
        print(f"{indent}Per instrument (the same error on nearly every note = synth_root off by that interval)")
        for v in res["instruments"]:
            if verdict_text(v):
                print(f"{indent}  inst {v['instrument']:>2}: ok {v['ok']:>4}  wrong {v['notes'] - v['ok']:>4}   {verdict_text(v)}")
