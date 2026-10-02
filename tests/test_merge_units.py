"""Unit tests for the merge primitives — the rules this session's regressions hid in.

    python -m unittest discover -s tests -v

Each test builds its objects by hand (no song, no chip render), so it runs in milliseconds
and pins one rule: a looped follower is unrolled under a short primary, a key-off a tick
before the primary's end is no key-off, a transposed chord shares its composite, a bank
member is 256-byte aligned and cut where its sound ends, a MOD narrows only when the
columns beyond are empty, a YAML key given twice is refused, a unison chord is its primary
louder (no composite), a composite with a same-shape twin gives up its slot to the twin that
rings furthest.
"""

from __future__ import annotations

import io
import math
import sys
import unittest
from pathlib import Path
from typing import ClassVar

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio import RELEASE_FLOOR_DB, limit_peaks, saturate
from core.config import MergeGroup, format_patterns, load_yaml, parse_patterns
from core.config.entries import _parse_instrument_range, _parse_merge_group
from core.merge import (
    ALIGN,
    CHIP,
    MIX,
    Composite,
    CompositeKey,
    MergePlan,
    MixLayerKey,
    NoteOn,
    bank_reserve_wanted,
    composite_key,
    drop_composite,
    keyoff_secs,
    mix_pcm_composites,
    pack_banks,
    stand_in,
    trigger_note,
    unison_gain_db,
)
from core.merge.mix import _cut_layer
from core.merge.slots import _plan_slots, same_shape_twins
from core.mod import PAL_AMIGA_CLOCK, PERIOD_TABLE, ModFile, ModSample

CLOCK = float(PAL_AMIGA_CLOCK)


def _note(tick=0, duration=8, inst=4, index=24, kind="FM", secs=0.2, fill=0, fill_secs=None, **kw) -> NoteOn:
    n = NoteOn(tick, duration, duration, inst, index, kind, chip=index, secs=secs, fill=fill, fill_secs=fill_secs, **kw)
    n.ticks = [tick]
    return n


def _sample(data: bytes, volume=64, loop=None) -> ModSample:
    s = ModSample("t")
    s.data = data
    s.length = len(data) // 2
    s.set_volume(volume)
    if loop:
        s.repeat, s.repeat_length = loop[0] // 2, loop[1] // 2
    return s


class KeyOffRules(unittest.TestCase):
    def _keyoff(self, p: NoteOn, f: NoteOn, tolerance: int = 1) -> float:
        """keyoff_secs where the test expects a key-off."""
        secs = keyoff_secs(p, f, tolerance=tolerance)
        assert secs is not None
        return secs

    def test_fill_keys_the_follower_off(self):
        p, f = _note(secs=0.4, duration=16), _note(secs=0.4, duration=16, fill=4, fill_secs=4 / 60)
        self.assertAlmostEqual(self._keyoff(p, f), 4 / 60)

    def test_shorter_follower_keys_off_at_its_duration(self):
        p, f = _note(secs=0.4, duration=16), _note(secs=0.2, duration=8)
        self.assertAlmostEqual(self._keyoff(p, f), 0.2)

    def test_a_tick_short_of_the_primary_is_no_key_off(self):
        p, f = _note(secs=0.4, duration=16), _note(secs=0.375, duration=15)
        self.assertIsNone(keyoff_secs(p, f, tolerance=1))
        self.assertAlmostEqual(self._keyoff(p, f, tolerance=0), 0.375)


class CompositeKeys(unittest.TestCase):
    def test_transposed_chord_shares_the_key(self):
        p1, f1 = _note(index=16), _note(index=23, inst=14)
        p2, f2 = _note(index=14), _note(index=21, inst=14)
        k1 = composite_key(p1, [f1], False, lambda n: 1.0)
        k2 = composite_key(p2, [f2], False, lambda n: 1.0)
        self.assertEqual(k1, k2)
        self.assertEqual(k1.layers[0].interval, 7)   # the follower's interval, not its note

    def test_trigger_note_follows_the_transposition(self):
        c = Composite(-1, CompositeKey(MIX, 4, ()), MergeGroup("FM5", ["FM3"]), base=16, note=23)
        self.assertEqual(trigger_note(c, 16), 23)
        self.assertEqual(trigger_note(c, 14), 21)


def _gain(p, fs, chip) -> float:
    g = unison_gain_db(p, fs, chip=chip)
    assert g is not None
    return g


class Unison(unittest.TestCase):
    def test_same_voice_same_pitch_is_the_primary_louder(self):
        p, f = _note(voice=5), _note(voice=5)
        self.assertAlmostEqual(_gain(p, [f], True), 20 * math.log10(2))
        quieter = _note(voice=5, tl=4)                      # 4 TL steps = 3 dB down
        self.assertAlmostEqual(_gain(p, [quieter], True), 20 * math.log10(1 + 10 ** (-3 / 20)))

    def test_anything_else_is_a_composite(self):
        p = _note(voice=5)
        self.assertIsNone(unison_gain_db(p, [_note(voice=5, detune=2)], chip=True))       # chorus
        self.assertIsNone(unison_gain_db(p, [_note(voice=5, index=27)], chip=True))       # a third
        self.assertIsNone(unison_gain_db(p, [_note(voice=4)], chip=True))                 # another voice
        self.assertIsNone(unison_gain_db(p, [_note(voice=5, fill=4, fill_secs=4 / 60)], chip=True))
        self.assertIsNone(unison_gain_db(p, [_note(voice=5), _note(voice=5, index=31)], chip=True))

    def test_mixed_unison_uses_the_note_levels(self):
        p, f = _note(kind="PSG", inst=17, level_db=-2.0), _note(kind="PSG", inst=17, level_db=-8.0)
        self.assertAlmostEqual(_gain(p, [f], False), 20 * math.log10(1 + 10 ** (-6 / 20)))
        self.assertIsNone(unison_gain_db(p, [_note(kind="PSG", inst=18)], chip=False))


class Twins(unittest.TestCase):
    def _plan(self):
        g = MergeGroup("FM5", ["FM3", "PSG1"])
        cut = Composite(-1, CompositeKey(MIX, 14, (MixLayerKey(14, 7, 1.0, None), MixLayerKey(19, 0, 1.0, 267))), g,
                        base=16, note=23, notes=11, entry=[-1, "cut", 64, 0])
        held = Composite(-2, CompositeKey(MIX, 14, (MixLayerKey(14, 7, 1.0, None), MixLayerKey(19, 0, 1.0, None))), g,
                         base=14, note=21, notes=2, entry=[-2, "held", 64, 0])
        other = Composite(-3, CompositeKey(MIX, 14, (MixLayerKey(14, 4, 1.0, None),)), g, base=14, notes=1,
                          entry=[-3, "other", 64, 0])
        plan = MergePlan([g], composites={c.key: c for c in (cut, held, other)})
        plan.ticks = {("FM5", 0): -1, ("FM5", 8): -2, ("FM5", 16): -3}
        plan.bases = {("FM5", 0): 16, ("FM5", 8): 14, ("FM5", 16): 14}
        return plan, cut, held, other

    def test_the_twin_that_rings_further_is_kept(self):
        plan, cut, held, other = self._plan()
        twins = same_shape_twins(plan, [cut, held, other])
        self.assertEqual(twins, {-1: held.key})                # the PSG cut goes, however played
        self.assertNotIn(-3, twins)                            # another shape: no twin

    def test_a_twin_off_the_mod_range_is_not_one(self):
        plan, cut, held, other = self._plan()
        plan.bases[("FM5", 0)] = 30                            # 30 + 7 is past B3 on the kept mix
        self.assertEqual(same_shape_twins(plan, [cut, held, other]), {})

    def test_a_dropped_twin_plays_the_preferred_survivor(self):
        plan, cut, held, other = self._plan()

        class Cfg:
            sample_list: ClassVar[list] = [cut.entry, held.entry, other.entry]
        drop_composite(plan, Cfg, cut, "no free instrument slot", prefer=held.key)
        stand_in(plan)
        self.assertEqual(plan.ticks[("FM5", 0)], -2)
        self.assertEqual(plan.notes[("FM5", 0)], 23)           # held's trigger, moved to this note
        self.assertEqual(held.notes, 3)                        # one note-on handed over
        self.assertEqual(plan.unsupported[0]['stand_in'], -2)


class Mixer(unittest.TestCase):
    def _plan(self, longest: float) -> tuple[MergePlan, ModFile, Composite]:
        g = MergeGroup("DAC", ["FM2"])
        c = Composite(-1, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, note=None, longest=longest,
                      entry=[-1, "merge", 64, 0])
        plan = MergePlan([g], composites={c.key: c})
        mod = ModFile(4)
        mod.samples[0] = _sample(bytes([100] * 400))                           # the drum: 400 bytes, unlooped
        mod.samples[1] = _sample(bytes([0] * 100 + [90] * 200), loop=(100, 200))  # the bass: looped
        c.inst = 5
        return plan, mod, c

    def test_looped_follower_is_unrolled_for_the_composites_longest_note(self):
        plan, mod, _c = self._plan(longest=0.5)
        problems = mix_pcm_composites(plan, mod, CLOCK, hold_secs={}, padding_secs=0.0)
        self.assertEqual(problems, [])
        rate = CLOCK / PERIOD_TABLE[12]
        data = mod.samples[4].data
        self.assertGreaterEqual(len(data), int(0.5 * rate))        # not 2 bytes, not the drum's 400
        tail = [(b - 256 if b > 127 else b) for b in data[int(0.4 * rate):int(0.45 * rate)]]
        self.assertTrue(any(v != 0 for v in tail))                  # the bass is still there past the drum

    def test_layers_are_cut_to_the_composites_notes(self):
        plan, mod, _c = self._plan(longest=0.05)
        mix_pcm_composites(plan, mod, CLOCK, hold_secs={1: 9.0, 2: 9.0}, padding_secs=0.0)
        rate = CLOCK / PERIOD_TABLE[12]
        self.assertLessEqual(len(mod.samples[4].data), int(0.052 * rate) + 8)   # the notes plus a 2 ms fade

    def test_cut_layer_release_and_hard_cut(self):
        sig = [100.0] * 2000
        faded = _cut_layer(sig, 100, 1000.0, RELEASE_FLOOR_DB)   # the floor in one second at 1 kHz
        self.assertEqual(len(faded), 1100)
        self.assertLess(abs(faded[-1]), 1.0)
        hard = _cut_layer(sig, 100, 1000.0, None)               # 2 ms fade
        self.assertEqual(len(hard), 102)


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


class Limiter(unittest.TestCase):
    """limit_peaks: peaks past the ceiling come down to it, within max_db; the rest is untouched."""

    def test_a_burst_comes_down_to_the_ceiling(self):
        x = [60 * math.sin(2 * math.pi * 110 * i / 16574) for i in range(16574)]
        for k in range(4000, 4100):
            x[k] *= 2.6
        y, gr = limit_peaks(x, 16574, 127.0, 3.0)
        self.assertLessEqual(max(map(abs, y)), 127.0 + 1e-6)
        self.assertEqual(y[:3000], x[:3000])                      # before the lookahead: untouched
        self.assertGreater(gr, 1.5)

    def test_a_peak_in_the_first_millisecond(self):
        # a drum's loudest peak is its attack: the gain must already be down at sample 0
        x = [180.0 * math.exp(-i / 200) * math.sin(2 * math.pi * 60 * i / 16574) for i in range(4000)]
        x[3] = 160.0
        y, _gr = limit_peaks(x, 16574, 127.0, 4.0)
        self.assertLessEqual(max(map(abs, y)), 127.0 + 1e-6)

    def test_max_db_keeps_the_excess(self):
        x = [0.0] * 1000
        x[500] = 200.0
        y, gr = limit_peaks(x, 16574, 127.0, 1.0)
        self.assertAlmostEqual(gr, 1.0, places=6)
        self.assertGreater(max(map(abs, y)), 127.0)


class Saturate(unittest.TestCase):
    """saturate: the RMS rises the dB asked at the same peak; 0 dB is the identity."""

    def test_the_body_rises_the_gain_asked(self):
        rate = 8287
        x = [127 * math.exp(-i / (0.03 * rate)) * math.sin(2 * math.pi * 55 * i / rate) for i in range(rate // 4)]
        y = saturate(x, 2.0)

        def rms(v):
            return math.sqrt(sum(s * s for s in v) / len(v))
        self.assertAlmostEqual(20 * math.log10(rms(y) / rms(x)), 2.0, places=2)
        self.assertAlmostEqual(max(map(abs, y)), max(map(abs, x)), places=6)
        self.assertEqual(saturate(x, 0.0), x)


class BankReserve(unittest.TestCase):
    """merge_bank_slots: auto - how many slots the next build should hold back for banks."""

    def _want(self, banks, overflow, slotted, idle=()):
        g = MergeGroup("FM1", ["PSG2"])
        plan = MergePlan([g])
        plan.banks = [object()] * banks
        plan.bank_overflow = list(overflow)
        for i, n in enumerate(slotted, 1):
            c = Composite(20 + i, CompositeKey(MIX, 1, (MixLayerKey(2, i, 1.0, None),)), g, notes=n)
            plan.composites[c.key] = c

        return bank_reserve_wanted(plan, list(idle))

    def test_an_idle_slot_goes_back(self):
        self.assertEqual(self._want(2, [], [5, 1], idle=[19]), 2)

    def test_a_bank_that_carries_more_notes_than_the_composites_it_displaces(self):
        self.assertEqual(self._want(2, [12], [1, 3, 9]), 3)             # 12 bank notes > 1
        self.assertIsNone(self._want(2, [1], [4, 9]))                   # 1 bank note < 4: keep the slot
        self.assertEqual(self._want(2, [12, 1], [1, 3]), 3)             # the second bank does not pay

    def test_nothing_to_change(self):
        self.assertIsNone(self._want(2, [], [4]))


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


class Heard(unittest.TestCase):
    def _mix(self, heard: list) -> int:
        g = MergeGroup("DAC", ["FM2"])
        c = Composite(5, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, longest=1.0,
                      entry=[5, "merge", 64, 0], heard=heard)
        plan = MergePlan([g], composites={c.key: c})
        mod = ModFile(4)
        mod.samples[0] = _sample(bytes([100] * 20000))                     # the drum: long, unlooped
        mod.samples[1] = _sample(bytes([60] * 20000))
        mix_pcm_composites(plan, mod, CLOCK, hold_secs={}, padding_secs=0.0)
        return len(mod.samples[4].data)

    def test_the_mix_ends_where_the_next_note_on_cuts_every_note(self):
        rate = CLOCK / PERIOD_TABLE[12]
        whole = self._mix([])
        cut = self._mix([(0.5, 0.2, 1.0), (0.5, 0.3, 1.0)])                # next note-ons 0.2 / 0.3 s in
        self.assertLess(cut, whole)
        self.assertAlmostEqual(cut, 0.3 * rate, delta=0.003 * rate)        # the later one, plus a 2 ms fade

    def test_a_transposed_note_needs_more_of_the_mix(self):
        rate = CLOCK / PERIOD_TABLE[12]
        cut = self._mix([(0.5, 0.2, 2.0)])                                 # an octave up: twice the bytes
        self.assertAlmostEqual(cut, 0.4 * rate, delta=0.003 * rate)


class Slots(unittest.TestCase):
    def test_chip_composite_never_takes_an_fm_source_slot(self):
        g = MergeGroup("FM5", ["FM4"])
        chip = Composite(-1, CompositeKey(CHIP, 4, ()), g, notes=9)
        chip.fm = object()
        pcm = Composite(-2, CompositeKey(MIX, 4, ()), g, notes=1)
        chosen, left = _plan_slots([chip, pcm], [14, 20], pcm_only={14})
        self.assertEqual(chosen, {-1: 20, -2: 14})
        self.assertEqual(left, [])
        chosen, left = _plan_slots([chip], [14], pcm_only={14})
        self.assertEqual(left, [chip])


class MixLoop(unittest.TestCase):
    def _mix(self, loop_mix: bool) -> ModSample:
        rate = CLOCK / PERIOD_TABLE[12]
        period = 64                                             # a steady tone, 64 samples a cycle
        tone = bytes(int(90 * math.sin(2 * math.pi * i / period)) & 0xFF for i in range(int(3 * rate)))
        g = MergeGroup("FM1", ["PSG2"], loop_mix=loop_mix, loop_drift_db=1.0, loop_min_ms=300)
        c = Composite(5, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 0.5, None),)), g, base=12, longest=2.5,
                      entry=[5, "merge", 64, 0], pitch_hz=rate / period)
        mod = ModFile(4)
        mod.samples[0] = _sample(tone)
        mod.samples[1] = _sample(tone)
        mix_pcm_composites(MergePlan([g], composites={c.key: c}), mod, CLOCK, hold_secs={}, padding_secs=0.0)
        return mod.samples[4]

    def test_a_settled_mix_loops_and_is_shorter(self):
        plain, looped = self._mix(False), self._mix(True)
        self.assertLessEqual(plain.repeat_length, 1)
        self.assertGreater(looped.repeat_length * 2, 0.3 * CLOCK / PERIOD_TABLE[12] - 4)   # loop_min_ms
        self.assertLess(len(looped.data), len(plain.data))


class HeardPadding(unittest.TestCase):
    def test_what_a_note_can_still_hear_past_its_sustain(self):
        from core.audio import RELEASE_FLOOR_DB, fade_end, heard_padding
        self.assertEqual(heard_padding(0.5, 300.0, slides=False), 0.0)          # cut with C00
        self.assertAlmostEqual(heard_padding(0.5, 300.0, slides=True), RELEASE_FLOOR_DB / 300.0)
        self.assertEqual(heard_padding(0.5, 50.0, slides=True), 0.5)            # a slow release: the padding
        self.assertEqual(heard_padding(0.5, None, slides=True), 0.0)            # no release: a cut
        out = fade_end([1.0] * 100, 60, 10000)
        self.assertEqual(len(out), 60)
        self.assertEqual(out[0], 1.0)
        self.assertEqual(out[-1], 0.0)


class ModWriter(unittest.TestCase):
    def test_every_sample_takes_exactly_the_bytes_its_header_declares(self):
        mod = ModFile(4)
        odd = ModSample("odd")
        odd.data = bytes([7] * 5)
        odd.length = 3                                   # 5 bytes, evened to 3 words
        mod.samples[0] = odd
        nxt = ModSample("next")
        nxt.data = bytes([9] * 4)
        nxt.length = 2
        mod.samples[1] = nxt
        data = mod.get_bytes()
        end = len(data)
        self.assertEqual(data[end - 4:], bytes([9] * 4))   # the next sample starts on its own word
        self.assertEqual(data[end - 10:end - 4], bytes([7] * 5) + bytes(1))


    def test_a_one_shot_starts_with_a_silent_word(self):
        # ProTracker replays a one-shot's first word after it ends; a looped sample replays its loop
        mod = ModFile(4)
        one_shot, looped = _sample(bytes([0x82, 0x7E, 5, 6])), _sample(bytes([0x82, 0x7E, 5, 6]), loop=(0, 4))
        mod.samples[0], mod.samples[1] = one_shot, looped
        mod.zero_idle_words()
        self.assertEqual(one_shot.data, bytes([0, 0, 5, 6]))
        self.assertEqual(looped.data, bytes([0x82, 0x7E, 5, 6]))


class Dither(unittest.TestCase):
    """to_int8's modes: off rounds, flat and shaped add noise, shaped spends it at the top."""

    def test_modes(self):
        from core.audio import DITHER_FLAT, DITHER_OFF, DITHER_SHAPED, signed8, to_int8
        x = [60 * math.sin(2 * math.pi * 200 * i / 16574) for i in range(8192)]
        self.assertEqual(signed8(to_int8(x, 1.0, DITHER_OFF)), [math.floor(v + 0.5) for v in x])

        def hf(mode: str) -> float:
            # error power in the second difference: noise near Nyquist
            e = [q - v for q, v in zip(signed8(to_int8(x, 1.0, mode)), x, strict=True)]
            return sum((e[i] - e[i - 1]) ** 2 for i in range(1, len(e)))
        self.assertLess(hf(DITHER_FLAT), hf(DITHER_SHAPED))
        with self.assertRaises(ValueError):
            to_int8(x, 1.0, "noisy")

    def test_dc_block_centres_a_lopsided_wave_and_keeps_silence_at_zero(self):
        from core.audio import dc_block
        rate = 16574
        wave = [0.0] * 100 + [40 + 60 * math.sin(2 * math.pi * 220 * i / rate) for i in range(rate)]
        out = dc_block(wave, rate)
        self.assertEqual(out[:100], [0.0] * 100)                     # leading silence untouched
        tail = out[-rate // 4:]
        self.assertLess(abs(sum(tail) / len(tail)), 0.5)             # the +40 offset gone
        self.assertGreater(max(tail), 55)                            # the 220 Hz wave kept

    def test_a_mix_falls_back_to_its_primary_entry(self):
        from core.merge import composite_dither
        key = CompositeKey(MIX, 10, (MixLayerKey(15, 0, 1.0, None),))
        plain = Composite(23, key, MergeGroup("FM1", ["PSG2"]))
        self.assertEqual(composite_dither(plain, {10: "off"}, "shaped"), "off")      # the primary's entry
        self.assertEqual(composite_dither(plain, {15: "off"}, "shaped"), "shaped")   # a follower's is not
        own = Composite(23, key, MergeGroup("FM1", ["PSG2"], dither="flat"))
        self.assertEqual(composite_dither(own, {10: "off"}, "shaped"), "flat")       # the group's wins

    def test_an_entry_and_a_group_may_override(self):
        e = _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 9, "dither": "Flat"})
        self.assertEqual(e.dither, "flat")
        g = _parse_merge_group({"primary": "FM3", "followers": ["FM4"], "dither": False}, "t")
        self.assertEqual(g.dither, "off")                    # YAML reads a bare `off` as false
        with self.assertRaises(ValueError):
            _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 9, "dither": "noisy"})


class Narrowing(unittest.TestCase):
    def test_narrow_only_when_the_columns_beyond_are_empty(self):
        mod = ModFile(8)
        mod.set_channel(1)
        from core.mod import ModNote
        mod.set_note(ModNote.C2, 1)
        self.assertEqual(mod.used_channels(), 2)
        mod.narrow_to(4)
        self.assertEqual(mod.CHANNELS, 4)
        self.assertEqual(mod.MOD_FORMAT, b"M.K.")
        self.assertEqual(mod.note_at(0, 0, 1), PERIOD_TABLE[ModNote.C2.value])
        wide = ModFile(8)
        wide.set_channel(5)
        wide.set_note(ModNote.C2, 1)
        with self.assertRaises(ValueError):
            wide.narrow_to(4)


class ConfigLoading(unittest.TestCase):
    def test_loop_overrides_on_an_entry_and_a_group(self):
        e = _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 11, "root": "C2",
                                     "loop_drift_db": 1, "loop_min_ms": 250})
        self.assertEqual((e.loop_drift_db, e.loop_min_ms), (1.0, 250.0))
        g = _parse_merge_group({"primary": "FM3", "followers": ["FM4"], "loop_min_ms": 400}, "t")
        self.assertEqual(g.loop_min_ms, 400.0)
        with self.assertRaises(ValueError):
            _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 11, "loop_drift_db": -1})
        with self.assertRaises(ValueError):
            _parse_merge_group({"primary": "FM3", "followers": ["FM4"], "loop_min_ms": 0}, "t")

    def test_sample_settings_read_from_samples(self):
        import tempfile

        from core.config import PsgSynthesisSettings, SynthesisSettings

        def load(text: str) -> SynthesisSettings:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                f.write(text)
            try:
                return SynthesisSettings.from_yaml(f.name)
            finally:
                Path(f.name).unlink()

        s = load("samples:\n  max_sample_kb: 64\n  pt_zero_bytes: false\n")
        self.assertEqual((s.max_sample_kb, s.pt_zero_bytes), (64, False))
        # a key left out is the field's default
        self.assertEqual(load("{}\n"), SynthesisSettings())
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("{}\n")
        try:
            self.assertEqual(PsgSynthesisSettings.from_yaml(f.name), PsgSynthesisSettings())
        finally:
            Path(f.name).unlink()
        for text in ("samples:\n  pt_zero_byte: false\n",       # a typo is not ignored
                     "max_sample_kb: 64\n",                      # nor a key outside its section
                     "fm_synthesis:\n  headroom_db: 3\n"):      # nor a retired one
            with self.assertRaises(ValueError):
                load(text)

    def test_duplicate_key_is_refused(self):
        text = "merge_patterns:\n  - patterns: '1'\n    groups:\n      - primary: FM3\n        followers: [FM4]\n        primary: FM5\n"
        with self.assertRaises(ValueError) as cm:
            load_yaml(io.StringIO(text))
        self.assertIn("'primary'", str(cm.exception))

    def test_patterns_are_hex_ranges(self):
        pats = parse_patterns("0, 5-c, d-10", "t")
        self.assertEqual(sorted(pats), [0, *range(5, 17)])
        self.assertEqual(format_patterns(pats), "0, 5-10")
        self.assertEqual(parse_patterns(10, "t"), frozenset({10}))


if __name__ == "__main__":
    unittest.main()
