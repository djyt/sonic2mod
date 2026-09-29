"""Sample banks: several composite sounds in one MOD instrument, each chosen with `9xx`.

A merged build that folds the bass onto the drum channel needs one mixed sample per (drum,
bass note, hat) combination, and the 31 instrument slots run out long before the sounds do.
A MOD note can start anywhere in its sample (`9xx`: the offset in 256-byte units, up to
$FF00), so the mixed composites of a `bank: true` group are laid end to end in as few slots as
they fit, each sound aligned to 256 bytes and followed by a little silence, and every note
starts with `9xx` at its sound's offset.  The sound would run on into the next one, so the
converter cuts the note once the sound is over (`MergePlan.region_at`, the same `ECx` / `C00`
a note fill writes).  This is for a drum primary: its notes carry no other command, so the
effect slot is free for the offset (a delayed note gives up its `EDx`).

What a bank cannot hold: a looped sample (one loop header per slot), and sounds with different
finetunes (one per slot as well).  A bank plays at one volume — the loudest member's — and
the quieter members are scaled into their bytes.  A member that fits in no bank (the slots
are gone, or it is longer than a sample may be) is dropped like any composite over budget:
a surviving composite of the same shape stands in for it, else the primary plays alone.

The packing happens after the mixes are made (their real sizes are known then) into the
slots the composite fit left free (`MergePlan.spare_slots`, at least `merge_bank_slots`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .merge import Composite, MergePlan, drop_composite, stand_in
from .mod import ModSample
from .pcm import signed8, to_int8
from .tables import PERIOD_TABLE

MAX_OFFSET = 0xFF00      # the last sound start 9xx can name (xx × 256)
ALIGN = 256              # a sound starts on a 9xx boundary


@dataclass
class Bank:
    slot: int
    volume: int
    finetune: int
    members: list[Composite] = field(default_factory=list)
    data: bytearray = field(default_factory=bytearray)

    @property
    def bytes(self) -> int:
        return len(self.data)

    def fits(self, region: int, finetune: int, max_bytes: int) -> bool:
        return (self.finetune == finetune and self.bytes <= MAX_OFFSET
                and self.bytes + region <= max_bytes)


def pack_banks(plan: MergePlan, config, mod, samples: dict[int, ModSample], slots: list[int],
               max_bytes: int, pad_secs: float, amiga_clock: float,
               raw: dict[int, list[float]] | None = None) -> list[dict]:
    """Lay the banked composites' samples (`samples`, by provisional id, from the mixer) into
    banks in `slots`, install the banks in `mod`, and point the plan at them: the members'
    ids become their bank's slot and `plan.regions` says where each note's sound starts and
    how long it is.  `pad_secs` (one MOD tick) of silence follows every sound, so the cut the
    converter places, up to half a tick off, never reaches the next sound.  The most-played
    composites are packed first.  A member whose normalised sum the mixer kept (`raw`) is
    quantised here, once, with the bank's volume scaling in; the others' bytes are scaled.
    Returns one dict per member dropped."""
    raw = raw or {}
    members = sorted((c for c in plan.composites.values() if c.banked and c.inst in samples),
                     key=lambda c: (-c.notes, c.inst))
    if not members:
        return []
    volume = max(samples[c.inst]._volume for c in members)
    banks: list[Bank] = []
    dropped: list[dict] = []
    pool = list(slots)
    for c in members:
        s = samples[c.inst]
        rate = amiga_clock / PERIOD_TABLE[c.note if c.note is not None else c.base]
        sound = len(s.data)
        region = -(-(sound + math.ceil(rate * pad_secs)) // ALIGN) * ALIGN
        why = None
        if s.repeat_length > 1:
            why = "a looped sample cannot share a slot"
        elif region > max_bytes:
            why = f"its {sound} bytes do not fit a sample"
        else:
            bank = next((b for b in banks if b.fits(region, s._finetune, max_bytes)), None)
            if bank is None:
                if not pool:
                    why = "no slot left for another bank (merge_bank_slots)"
                else:
                    bank = Bank(pool.pop(0), volume, s._finetune)
                    banks.append(bank)
        if why is not None:
            dropped.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail, 'reason': why})
            drop_composite(plan, config, c, why)
            continue
        assert bank is not None
        c.offset, c.region = bank.bytes, sound
        if c.inst in raw:
            data = to_int8(raw[c.inst], s._volume / volume)
        elif s._volume == volume:
            data = s.data
        else:
            data = to_int8([v * s._volume / volume for v in signed8(s.data)], 1.0)
        # Padded to its region from what was quantised: a raw sum can be a byte shorter than the
        # sample (an odd length evened with a zero), and every later sound then started before its
        # 256-byte boundary, the 9xx rounding down onto up to 255 bytes of silence (23 ms at 11 kHz)
        bank.data += data + bytes(region - len(data))
        bank.members.append(c)
    stand_in(plan)                                       # a dropped member's notes, if a shape survives
    sample_list = config.sample_list if config.sample_list is not None else []
    for i, bank in enumerate(banks, 1):
        sample = ModSample(f"bank {bank.members[0].group.primary} {i}")
        sample.data = bytes(bank.data)
        sample.length = len(sample.data) // 2
        sample.set_volume(bank.volume)
        sample._finetune = bank.finetune
        mod.samples[bank.slot - 1] = sample
        for c in bank.members:
            for key in [k for k, v in plan.ticks.items() if v == c.inst]:
                plan.ticks[key] = bank.slot
                plan.regions[key] = (c.offset, c.region)
            if c.entry in sample_list:
                sample_list.remove(c.entry)
            c.inst = bank.slot
        sample_list.append([bank.slot, sample._name, bank.volume, bank.finetune])
    plan.banks = banks
    return dropped
