"""Sample banks (core/merge/banks.py): a member is 256-byte aligned and cut where its sound ends.

    python -m pytest tests/core/merge/test_banks.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import MergeGroup
from core.merge import (
    ALIGN,
    MIX,
    Composite,
    CompositeKey,
    MergePlan,
    MixLayerKey,
    pack_banks,
)
from core.mod import PAL_AMIGA_CLOCK, ModFile, ModSample

CLOCK = float(PAL_AMIGA_CLOCK)


def _sample(data: bytes, volume=64, loop=None) -> ModSample:
    s = ModSample("t")
    s.data = data
    s.length = len(data) // 2
    s.set_volume(volume)
    if loop:
        s.repeat, s.repeat_length = loop[0] // 2, loop[1] // 2
    return s


class Banks(unittest.TestCase):
    def test_pack_aligns_cuts_and_drops(self):
        g = MergeGroup("DAC", ["FM2"], bank=True)
        c1 = Composite(-1, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, note=None, banked=True, notes=5,
                       entry=[-1, "a", 64, 0])
        c2 = Composite(-2, CompositeKey(MIX, 1, (MixLayerKey(2, 3, 1.0, None),)), g, base=12, note=None, banked=True, notes=1,
                       entry=[-2, "b", 32, 0])
        plan = MergePlan([g], composites={c1.key: c1, c2.key: c2})
        plan.ticks = {("DAC", 0): -1, ("DAC", 8): -2}
        plan.bases = {("DAC", 0): 12, ("DAC", 8): 12}

        class Cfg:
            sample_list: ClassVar[list] = [c1.entry, c2.entry]
        mod = ModFile(4)
        samples = {-1: _sample(bytes([50] * 1000)), -2: _sample(bytes([50] * 300), volume=32)}
        raw = {-1: [50.0] * 1000, -2: [50.0] * 300}
        dropped = pack_banks(plan, Cfg, mod, samples, [7], max_bytes=4096, pad_secs=0.0, amiga_clock=CLOCK, raw=raw)
        self.assertEqual(dropped, [])
        self.assertEqual(c1.inst, 7)
        self.assertEqual(c1.offset % ALIGN, 0)
        self.assertEqual(c2.offset % ALIGN, 0)
        self.assertEqual(plan.regions[("DAC", 0)], (c1.offset, 1000))
        self.assertEqual(plan.regions[("DAC", 8)], (c2.offset, 300))
        self.assertEqual(mod.samples[6].volume, 64)                # the loudest member's
        self.assertEqual(len(mod.samples[6].data) % ALIGN, 0)
        # a member that fits no bank is dropped and reported
        c3 = Composite(-3, CompositeKey(MIX, 1, (MixLayerKey(2, 5, 1.0, None),)), g, base=12, note=None, banked=True, notes=1,
                       entry=[-3, "c", 64, 0])
        plan.composites[c3.key] = c3
        plan.ticks[("DAC", 16)] = -3
        Cfg.sample_list.append(c3.entry)
        dropped = pack_banks(plan, Cfg, mod, {-3: _sample(bytes([50] * 5000))}, [], max_bytes=4096,
                             pad_secs=0.0, amiga_clock=CLOCK, raw={})
        self.assertEqual(len(dropped), 1)
        self.assertNotIn(("DAC", 16), plan.ticks)


    def test_a_quieter_member_is_quantised_once_from_its_sum(self):
        # scaling its 8-bit bytes would dither them twice: without the sum it is refused
        g = MergeGroup("DAC", ["FM2"], bank=True)
        loud = Composite(-1, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, banked=True, notes=2,
                         entry=[-1, "a", 64, 0])
        quiet = Composite(-2, CompositeKey(MIX, 1, (MixLayerKey(2, 3, 1.0, None),)), g, base=12, banked=True, notes=1,
                          entry=[-2, "b", 32, 0])
        plan = MergePlan([g], composites={loud.key: loud, quiet.key: quiet})
        plan.ticks = {("DAC", 0): -1, ("DAC", 8): -2}

        class Cfg:
            sample_list: ClassVar[list] = [loud.entry, quiet.entry]
        samples = {-1: _sample(bytes([100] * 512)), -2: _sample(bytes([100] * 512), volume=32)}
        with self.assertRaises(ValueError):
            pack_banks(plan, Cfg, ModFile(4), samples, [7], max_bytes=4096, pad_secs=0.0, amiga_clock=CLOCK, raw={})


class MelodicBanks(unittest.TestCase):
    """A melodic primary's mixes bank too: a looped member goes last, each bank plays at its
    own loudest member's volume, and the banks the slots cannot hold are counted."""

    def _plan(self, specs):
        g = MergeGroup("FM2", ["PSG1"], bank=True)
        comps, ticks, samples, entries = {}, {}, {}, []
        for i, (size, vol, loop, notes) in enumerate(specs, 1):
            c = Composite(-i, CompositeKey(MIX, 3, (MixLayerKey(16, i, 1.0, None),)), g, base=12, banked=True,
                          notes=notes, entry=[-i, f"m{i}", vol, 0])
            comps[c.key] = c
            ticks[("FM2", 8 * i)] = -i
            samples[-i] = _sample(bytes([40] * size), volume=vol, loop=loop)
            entries.append(c.entry)

        class Cfg:
            sample_list: ClassVar[list] = entries
        plan = MergePlan([g], composites=comps)
        plan.ticks = ticks
        return plan, Cfg, samples

    def test_a_looped_member_goes_last_with_the_bank_loop(self):
        # the looped one is the most played, yet it is packed after the other
        plan, cfg, samples = self._plan([(1024, 64, (512, 256), 9), (512, 64, None, 1)])
        mod = ModFile(4)
        self.assertEqual(pack_banks(plan, cfg, mod, samples, [7], max_bytes=8192, pad_secs=0.0,
                                    amiga_clock=CLOCK, raw={}), [])
        looped = next(c for c in plan.composites.values() if c.looped)
        plain = next(c for c in plan.composites.values() if not c.looped)
        self.assertLess(plain.offset, looped.offset)
        bank = mod.samples[6]
        self.assertEqual(bank.repeat * 2, looped.offset + 512)           # the member's loop, in the bank
        self.assertEqual(bank.repeat_length * 2, 256)
        self.assertEqual(len(bank.data), looped.offset + 1024)          # nothing follows it
        self.assertIs(plan.bank_members[("FM2", 8)], looped)
        self.assertEqual(looped.bank_id, -1)                            # its notes' level key

    def test_a_bank_plays_at_its_own_loudest_member(self):
        # two banks (each sound fills one); the second holds only the quiet sound
        plan, cfg, samples = self._plan([(3000, 64, None, 5), (3000, 32, None, 1)])
        mod = ModFile(4)
        pack_banks(plan, cfg, mod, samples, [7, 8], max_bytes=4096, pad_secs=0.0, amiga_clock=CLOCK, raw={})
        self.assertEqual(mod.samples[6].volume, 64)
        self.assertEqual(mod.samples[7].volume, 32)                    # not scaled down to the drums' 64
        self.assertEqual(mod.samples[7].data[0], 40)                    # its bytes as they were

    def test_sounds_share_a_bank_with_their_own_volume(self):
        # first fit would pair each loud sound with a quiet one: [64 32] [64 32]
        plan, cfg, samples = self._plan([(1500, 64, None, 9), (1500, 32, None, 8), (1500, 64, None, 7),
                                         (1500, 32, None, 6)])
        mod = ModFile(4)
        raw = {i: [40.0] * len(smp.data) for i, smp in samples.items()}
        pack_banks(plan, cfg, mod, samples, [7, 8], max_bytes=3200, pad_secs=0.0, amiga_clock=CLOCK, raw=raw)
        by_slot = {}
        for c in plan.composites.values():
            by_slot.setdefault(c.inst, set()).add(c.member_volume)
        self.assertEqual(sorted(map(sorted, by_slot.values())), [[32], [64]])   # still two banks

    def test_banks_the_slots_cannot_hold_are_counted(self):
        plan, cfg, samples = self._plan([(3000, 64, None, 5), (3000, 64, None, 2), (3000, 64, None, 1)])
        mod = ModFile(4)
        dropped = pack_banks(plan, cfg, mod, samples, [7], max_bytes=4096, pad_secs=0.0, amiga_clock=CLOCK, raw={})
        self.assertEqual(len(dropped), 2)
        self.assertEqual(plan.bank_overflow, [2, 1])                    # notes of each bank left out


class BankAlignment(unittest.TestCase):
    def test_a_short_raw_member_keeps_the_next_sound_on_its_boundary(self):
        g = MergeGroup("DAC", ["PSG3"], bank=True)
        c1 = Composite(-1, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, banked=True, notes=2,
                       entry=[-1, "a", 64, 0])
        c2 = Composite(-2, CompositeKey(MIX, 1, (MixLayerKey(2, 3, 1.0, None),)), g, base=12, banked=True, notes=1,
                       entry=[-2, "b", 64, 0])
        plan = MergePlan([g], composites={c1.key: c1, c2.key: c2})
        plan.ticks = {("DAC", 0): -1, ("DAC", 8): -2}

        class Cfg:
            sample_list: ClassVar[list] = [c1.entry, c2.entry]
        mod = ModFile(4)
        # An odd sum is evened with a zero byte: its raw values are one short of the sample
        samples = {-1: _sample(bytes([40] * 1001)), -2: _sample(bytes([90] * 300))}
        raw = {-1: [40.0] * 1000, -2: [90.0] * 300}
        pack_banks(plan, Cfg, mod, samples, [7], max_bytes=8192, pad_secs=0.0, amiga_clock=CLOCK, raw=raw)
        self.assertEqual(c2.offset % ALIGN, 0)
        self.assertEqual(mod.samples[6].data[c2.offset], 90)           # 9xx lands on the sound, not silence


if __name__ == "__main__":
    unittest.main()
