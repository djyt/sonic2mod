"""Sample banks: several composite sounds in one MOD instrument, each chosen with `9xx`.

A merged build that folds the bass onto the drum channel needs one mixed sample per (drum,
bass note, hat) combination, and the 31 instrument slots run out long before the sounds do.
A MOD note can start anywhere in its sample (`9xx`: the offset in 256-byte units, up to
$FF00), so the mixed composites of a `bank: true` group are laid end to end in as few slots as
they fit, each sound aligned to 256 bytes and followed by a little silence, and every note
starts with `9xx` at its sound's offset.  The sound would run on into the next one, so the
converter cuts the note once the sound is over (`MergePlan.region_at`, the same `ECx` / `C00`
a note fill writes).  The offset takes the note's effect slot: a drum note carries nothing
else; a melodic one gives way where it must (a `Cxx` moves to the note's next row, an `EDx` is
dropped, a cut inside the attack row moves to the next one) and the converter counts them.

A bank has one loop header, so a looped member goes last in its bank with the loop pointing at
its own, and nothing follows it; its notes need no cut.  Sounds with different finetunes cannot
share a bank (one per slot).  A bank plays at the volume of its loudest member and the quieter
members are scaled into their bytes; each keeps its own volume (`Composite.member_volume`) and
its notes their own level (`Composite.bank_id`, the key the converter measures them under) and
release rate.  A member that fits in no bank (the slots are gone, or it is longer than a sample
may be) is dropped like any composite over budget: a surviving composite of the same shape
stands in for it, else the primary plays alone.  The banks the slots could not hold are counted
(`MergePlan.bank_overflow`, notes per bank) for convert()'s choice of how many slots to hold back.

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
    looped: bool = False                  # its last member loops: nothing may follow it
    loop: tuple[int, int] | None = None   # (start, length) in bytes of that loop within the bank

    @property
    def bytes(self) -> int:
        return len(self.data)

    def fits(self, region: int, finetune: int, max_bytes: int) -> bool:
        return (self.finetune == finetune and self.bytes <= MAX_OFFSET
                and self.bytes + region <= max_bytes)


def _region(c: Composite, sound: int, looped: bool, pad_secs: float, amiga_clock: float) -> int:
    """Bytes a member takes in its bank: its sound, then (unless it loops, the bank's last)
    `pad_secs` of silence at the rate it is triggered at, up to the next 256-byte boundary."""
    if looped:
        return sound
    rate = amiga_clock / PERIOD_TABLE[c.note if c.note is not None else c.base]
    return -(-(sound + math.ceil(rate * pad_secs)) // ALIGN) * ALIGN


def pack_banks(plan: MergePlan, config, mod, samples: dict[int, ModSample], slots: list[int],
               max_bytes: int, pad_secs: float, amiga_clock: float,
               raw: dict[int, list[float]] | None = None) -> list[dict]:
    """Lay the banked composites' samples (`samples`, by provisional id, from the mixer) into
    banks in `slots`, install the banks in `mod`, and point the plan at them: the members'
    ids become their bank's slot, `plan.regions` says where each note's sound starts and how
    long it is and `plan.bank_members` which member it plays.  `pad_secs` (one MOD tick) of
    silence follows every sound but a looped one, so the cut the converter places, up to half a
    tick off, never reaches the next sound.  The most-played composites are packed first and the
    looped ones last, each closing its bank.  A member whose normalised sum the mixer kept
    (`raw`) is quantised here, once, with its bank's volume scaling in; the others' bytes are
    scaled.  Returns one dict per member dropped."""
    raw = raw or {}
    members = sorted((c for c in plan.composites.values() if c.banked and c.inst in samples),
                     key=lambda c: (samples[c.inst].repeat_length > 1, -c.notes, c.inst))
    if not members:
        return []
    dropped: list[dict] = []

    def drop(c: Composite, why: str) -> None:
        dropped.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail, 'reason': why})
        drop_composite(plan, config, c, why)

    # Every member into the first bank it fits, as many banks as that takes
    banks: list[Bank] = []
    for c in members:
        s = samples[c.inst]
        looped = s.repeat_length > 1
        region = _region(c, len(s.data), looped, pad_secs, amiga_clock)
        if region > max_bytes:
            drop(c, f"its {len(s.data)} bytes do not fit a sample")
            continue
        bank = next((b for b in banks if not b.looped and b.fits(region, s._finetune, max_bytes)), None)
        if bank is None:
            bank = Bank(0, 0, s._finetune)
            banks.append(bank)
        c.offset, c.region, c.looped = bank.bytes, len(s.data), looped
        bank.data += bytes(region)              # its room; the bytes go in once the volume is known
        bank.looped = looped
        bank.members.append(c)

    # The slots hold the first banks (the most-played sounds); the others' members are dropped
    kept = banks[:len(slots)]
    plan.bank_overflow = [sum(c.notes for c in b.members) for b in banks[len(slots):]]
    for bank in banks[len(slots):]:
        for c in bank.members:
            drop(c, "no slot left for another bank (merge_bank_slots)")

    for slot, bank in zip(slots, kept, strict=False):
        bank.slot = slot
        bank.volume = max(samples[c.inst]._volume for c in bank.members)
        bank.data = bytearray()
        for c in bank.members:
            s = samples[c.inst]
            c.member_volume = s._volume
            if c.inst in raw:
                data = to_int8(raw[c.inst], s._volume / bank.volume)
            elif s._volume == bank.volume:
                data = s.data
            else:
                data = to_int8([v * s._volume / bank.volume for v in signed8(s.data)], 1.0)
            if c.looped:
                bank.loop = (c.offset + s.repeat * 2, s.repeat_length * 2)
                bank.data += data
                continue
            # Padded to its region from what was quantised: a raw sum can be a byte shorter than the
            # sample (an odd length evened with a zero), and every later sound then started before its
            # 256-byte boundary, the 9xx rounding down onto up to 255 bytes of silence (23 ms at 11 kHz)
            region = _region(c, c.region, False, pad_secs, amiga_clock)
            bank.data += data + bytes(region - len(data))

    stand_in(plan)                                       # a dropped member's notes, if a shape survives
    sample_list = config.sample_list if config.sample_list is not None else []
    for i, bank in enumerate(kept, 1):
        sample = ModSample(f"bank {bank.members[0].group.primary} {i}")
        sample.data = bytes(bank.data) + (b"\0" if len(bank.data) % 2 else b"")
        sample.length = len(sample.data) // 2
        sample.set_volume(bank.volume)
        sample._finetune = bank.finetune
        if bank.loop is not None:
            sample.repeat, sample.repeat_length = bank.loop[0] // 2, bank.loop[1] // 2
        mod.samples[bank.slot - 1] = sample
        for c in bank.members:
            c.bank_id = c.inst
            for key in [k for k, v in plan.ticks.items() if v == c.inst]:
                plan.ticks[key] = bank.slot
                plan.regions[key] = (c.offset, c.region)
                plan.bank_members[key] = c
            if c.entry in sample_list:
                sample_list.remove(c.entry)
            c.inst = bank.slot
        sample_list.append([bank.slot, sample._name, bank.volume, bank.finetune])
    plan.banks = kept
    return dropped
