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

A bank has one loop header, so it holds one looped member at most, laid out last with the loop
pointing at its own; its notes need no cut.  Sounds with different finetunes cannot
share a bank (one per slot).  A bank plays at the volume of its loudest member and the quieter
members are scaled into their bytes (8-bit range lost), so sounds are grouped by volume where
that takes no more banks than first fit (`_layout`); each keeps its own volume (`Composite.member_volume`) and
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

from .merge import Composite, MergePlan, composite_dither, drop_composite, stand_in
from .mod import ModSample
from .pcm import DEFAULT_DITHER, to_int8
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


def _loss_db(banks: list[Bank], volume: dict[int, int]) -> float:
    """dB of 8-bit range the layout costs, over every note: a member quieter than its bank's
    loudest is scaled down into its bytes."""
    return sum(c.notes * 20 * math.log10(max(volume[c.inst] for c in b.members) / volume[c.inst])
               for b in banks for c in b.members)


def _layout(members: list[Composite], sizes: dict[int, tuple[int, int, bool]], volume: dict[int, int],
            max_bytes: int, budget: int | None) -> list[Bank]:
    """Banks for `members` (sizes: {id: (region bytes, finetune, looped)}), at most one looped
    member each (it is laid out last).  `budget` None: each into the first bank it fits, the
    most-played first (loops last, as before).  Else
    the loudest first, each into a bank of its own volume, a new one while fewer than `budget`
    exist, or the fitting bank nearest its volume:

        first fit   [kick kick bass] [kick bass bass]   bass scaled to the kicks' 64
        by volume   [kick kick kick] [bass bass bass]   each bank at its members' volume
    """
    if budget is None:
        order = sorted(members, key=lambda c: (sizes[c.inst][2], -c.notes, c.inst))
    else:
        order = sorted(members, key=lambda c: (-volume[c.inst], -c.notes, c.inst))
    def open_to(bank: Bank, looped: bool) -> bool:
        # A bank holds one loop.  First fit places loops last, so nothing joins a looped bank; by
        # volume, a plain member still may (it is laid out before the loop), a second loop never
        return not bank.looped or (budget is not None and not looped)

    banks: list[Bank] = []
    for c in order:
        region, finetune, looped = sizes[c.inst]
        fits = [b for b in banks if open_to(b, looped) and b.fits(region, finetune, max_bytes)]
        v = volume[c.inst]

        # First fit: the first bank with room
        bank = fits[0] if fits and budget is None else None

        # By volume: its own volume's bank, a new one within the budget, else the nearest
        if budget is not None:
            same = [b for b in fits if b.volume == v]
            if same:
                bank = same[0]
            elif len(banks) >= budget and fits:
                bank = min(fits, key=lambda b: abs(math.log(b.volume / v)))
        if bank is None:
            bank = Bank(0, 0, finetune)
            banks.append(bank)
        bank.data += bytes(region)              # its room; the bytes go in once the layout is chosen
        bank.volume = max(bank.volume, v)
        bank.looped = bank.looped or looped
        bank.members.append(c)
    for bank in banks:                          # a loop runs to the end of the sample: last
        bank.members.sort(key=lambda c: sizes[c.inst][2])
    return banks


def _member_bytes(inst: int, s: ModSample, volume: int, raw: dict[int, list[float]], dither: str) -> bytes:
    """A member's bytes at its bank's `volume`, quantised once.

    The mixer's unquantised sum (`raw`) is scaled and quantised here; without one, the bytes are
    used as they are, which only holds at the bank's own volume (or for silence).  Scaling the
    8-bit bytes would dither them a second time and lose bits."""
    if inst in raw:
        return to_int8(raw[inst], s.volume / volume, dither)
    if s.volume in (volume, 0):
        return s.data
    raise ValueError(f"bank member {inst}: no unquantised sum to bring volume {s.volume} to its bank's {volume}")


def pack_banks(plan: MergePlan, config, mod, samples: dict[int, ModSample], slots: list[int],
               max_bytes: int, pad_secs: float, amiga_clock: float,
               raw: dict[int, list[float]], dither: str = DEFAULT_DITHER,
               entry_dithers: dict[int, str] | None = None) -> list[dict]:
    """Lay the banked composites' samples (`samples`, by provisional id, from the mixer) into
    banks in `slots`, install the banks in `mod`, and point the plan at them: the members'
    ids become their bank's slot, `plan.regions` says where each note's sound starts and how
    long it is and `plan.bank_members` which member it plays.  `pad_secs` (one MOD tick) of
    silence follows every sound but a looped one, so the cut the converter places, up to half a
    tick off, never reaches the next sound.  Sounds share a bank with sounds of their own volume
    where that takes no more banks than first fit (_layout); the looped ones go last, each
    closing its bank.  Short of slots, the banks with the fewest notes are left out.  A member
    whose normalised sum the mixer kept (`raw`) is quantised here, once, with its bank's volume
    scaling in (_member_bytes).  Returns one dict per member dropped."""
    members = sorted((c for c in plan.composites.values() if c.banked and c.inst in samples),
                     key=lambda c: (samples[c.inst].repeat_length > 1, -c.notes, c.inst))
    if not members:
        return []
    dropped: list[dict] = []

    def drop(c: Composite, why: str) -> None:
        dropped.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail, 'reason': why})
        drop_composite(plan, config, c, why)

    # Each member's room in a bank; one too big for any sample is dropped
    sizes: dict[int, tuple[int, int, bool]] = {}
    volume = {c.inst: samples[c.inst].volume for c in members}
    for c in list(members):
        s = samples[c.inst]
        looped = s.repeat_length > 1
        region = _region(c, len(s.data), looped, pad_secs, amiga_clock)
        if region > max_bytes:
            drop(c, f"its {len(s.data)} bytes do not fit a sample")
            members.remove(c)
            continue
        sizes[c.inst] = (region, s.finetune, looped)

    # By volume where it costs no bank more than first fit, and less range
    first = _layout(members, sizes, volume, max_bytes, None)
    by_volume = _layout(members, sizes, volume, max_bytes, len(first))
    banks = (by_volume if len(by_volume) <= len(first) and _loss_db(by_volume, volume) < _loss_db(first, volume)
             else first)

    # The slots hold the banks with the most notes; the others' members are dropped
    banks.sort(key=lambda b: -sum(c.notes for c in b.members))
    kept = banks[:len(slots)]
    plan.bank_overflow = [sum(c.notes for c in b.members) for b in banks[len(slots):]]
    for bank in banks[len(slots):]:
        for c in bank.members:
            drop(c, "no slot left for another bank (merge_bank_slots)")

    # Where each sound starts in its bank
    for bank in kept:
        at = 0
        for c in bank.members:
            region, _ft, looped = sizes[c.inst]
            c.offset, c.region, c.looped = at, len(samples[c.inst].data), looped
            at += region

    for slot, bank in zip(slots, kept, strict=False):
        bank.slot = slot
        bank.volume = max(samples[c.inst].volume for c in bank.members)
        bank.data = bytearray()
        for c in bank.members:
            s = samples[c.inst]
            c.member_volume = s.volume
            data = _member_bytes(c.inst, s, bank.volume, raw, composite_dither(c, entry_dithers or {}, dither))
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
        sample.set_finetune(bank.finetune)
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
        sample_list.append([bank.slot, sample.name, bank.volume, bank.finetune])
    plan.banks = kept
    return dropped
