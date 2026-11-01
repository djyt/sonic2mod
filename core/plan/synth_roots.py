"""The pitch each rooted map entry's sample is rendered at, from the song."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from ..config import PsgInstrumentEntry
from ..smps import ChannelType
from .conversion import ConversionPlan, RenderPitch
from .driver_state import enabled_channels, walk_channel

_MAX_SHIFT = 12     # semitones a sample is rendered above the pitch its root sounds, at most


@dataclass(frozen=True)
class SynthRoot:
    """One rooted map entry's rendering pitch, as resolve_synth_roots set it."""
    context: str                    # where the entry is (for the report)
    instrument: int
    synth_root: int | None
    derived: bool                   # from the song (False: the config states it)
    shift: int                      # its synth_shift
    votes: dict[int, int]           # {the pitch `root` stands for (D): notes}; more than one: ambiguous
    pitches: dict[int, int]         # {chip pitch: notes}


class ResolvedRoots(NamedTuple):
    """resolve_synth_roots' answer: what to report, and what the plan takes."""
    report: list[SynthRoot]               # one per rooted entry
    pitches: dict[int, RenderPitch]       # id(entry) -> where its sample is rendered (ConversionPlan.pitches)


def resolve_synth_roots(song, conv: ConversionPlan) -> ResolvedRoots:
    """Every rooted map entry's rendering pitch from the song: its `synth_root` and its shift.

    A sample is rendered at synth_root and played at root's rate, so MOD note m sounds at
    synth_root + (m − root).  A source note is placed at m = root + (key − low) (an anchored
    entry) or at its channel transpose (a rooted PSG entry without `low`), and the chip plays
    it at its real pitch (chip_pitch).  For the note to be in tune the sample must be rendered
    at D = chip pitch − (m − root): for an anchored entry that is the pitch the chip plays for
    `low`.  This walks every enabled FM/PSG channel with the DriverState, collects D for each
    note an entry routes, and

    - where the config leaves synth_root out, renders at the chip pitch the entry's notes play
      most often (ties to the lower one), at most an octave above D, shifted by that pitch − D.  Envelopes run in real time on the chip but stretch
      with playback rate in a MOD, so rendering at the busiest note keeps the most notes'
      attack and decay at the hardware's speed.  A config never needs to state it.
    - shifts by synth_root − D where it is stated (a rendering pitch anywhere in the range).

    The shift never moves a note: the sample generators give the sample a rate 2^(shift/12)
    higher than `root`'s playback rate, so MOD note `root` still sounds D and every note keeps
    its place, its playback rate and its bandwidth (placing notes lower instead halved the
    Chaos Emerald lead's rate to 4 kHz).  The cost is the sample's size, 2^(shift/12) times —
    hence the octave cap.

    Reports one SynthRoot per entry.  More than one D (`votes`) means the entry's
    low is played at several chip pitches (several pitch offsets or an
    smpsChangeTransposition under one source range — what `range_space: chip` and a split
    entry are for); the caller warns.
    """
    config = conv.config
    votes: dict[int, dict[int, int]] = {}       # id(entry) -> {D: notes}
    pitches: dict[int, dict[int, int]] = {}     # id(entry) -> {chip pitch: notes}
    for chan_cfg, channel in enabled_channels(song, config, (ChannelType.FM, ChannelType.PSG)):
        for _event, st, res in walk_channel(channel, conv, chan_cfg):
            if res is None:
                continue
            entry = res.entry
            if entry is None or entry.root is None:
                continue
            if st.is_psg and (st.in_noise_mode or (isinstance(entry, PsgInstrumentEntry) and entry.type != "tone")):
                continue
            m_rel = res.raw_index - entry.root.value      # m - root: anchored, or the transpose path
            per = votes.setdefault(id(entry), {})
            per[res.chip - m_rel] = per.get(res.chip - m_rel, 0) + 1
            pp = pitches.setdefault(id(entry), {})
            pp[res.chip] = pp.get(res.chip, 0) + 1

    def entries():
        for v, ranges in config.voice_map.items():
            for i, e in enumerate(ranges):
                yield f"voice_map[{v}][{i}]", e
        for src, vim in config.channel_instrument_map.items():
            for v, ranges in vim.items():
                for i, e in enumerate(ranges):
                    yield f"channel_instrument_map[{src}][{v}][{i}]", e
        for label, lst in config.psg_voice_map.items():
            for i, e in enumerate(lst):
                if e.type == "tone":
                    yield f"psg_voice_map[{label}][{i}]", e

    # Pass 1: what each entry needs on its own.
    items = []                                  # (context, entry, votes, D, stated)
    for context, e in entries():
        if e.root is None:
            continue
        per = votes.get(id(e), {})
        items.append((context, e, per, max(per, key=lambda d: per[d]) if per else None,
                      e.synth_root is not None))

    # Pass 2: one rendering pitch per instrument.  The sample generators render an
    # instrument once, for the first entry that names it (Credits folds voices onto 31 slots,
    # Stage Clear's PSG2 sits two octaves up its PSG1 sample), and a later entry's `root` is
    # written so that its notes play that sample in tune (make_credits_config.py) — so its own
    # D says nothing about the sample.  The pitch is chosen for the whole group: the chip
    # pitch their notes play most often, at most an octave above the first entry's D.
    # The octave cap on the shift, or the merged build's own (merge_max_synth_shift: the Amiga
    # port trades envelope timing at the busiest note for half the bytes)
    cap = _MAX_SHIFT
    if config.merge_active:
        cap = max(0, min(_MAX_SHIFT, config.merge_max_synth_shift))
    groups: dict[int, list] = {}
    for item in items:
        groups.setdefault(item[1].mod_instrument, []).append(item)
    out: dict[int, RenderPitch] = {}
    for group in groups.values():
        # The first entry with notes leads: a voice or envelope folded onto another's slot names it
        # too, and the first to name it may route none of its notes there (Streets of Rage 2's
        # Under Logic: adsr_..._v11's upper window holds only v12's notes)
        _, first, _, d_first, first_stated = next((item for item in group if item[3] is not None), group[0])
        if d_first is None:
            pitch, shift = first.synth_root, 0          # no notes: nothing to place
        elif first_stated:
            pitch = first.synth_root                    # the config says where the sample is
            shift = pitch - d_first
        else:
            counts: dict[int, int] = {}
            for _, e, _, d, _ in group:
                if d is None:
                    continue
                for chip, n in pitches.get(id(e), {}).items():
                    counts[chip] = counts.get(chip, 0) + n
            shift = max(0, min(max(counts, key=lambda c: (counts[c], -c)) - d_first, cap))
            pitch = d_first + shift
        for _, e, _, d, stated in group:
            if stated and e is not first:
                out[id(e)] = RenderPitch(e.synth_root, (e.synth_root - d) if d is not None else 0)   # its own say
            else:
                # One sample: an entry with no notes renders it as the lead does (the generator
                # renders for the first entry naming the slot)
                out[id(e)] = RenderPitch(pitch, shift if d is not None or d_first is not None else 0)

    report = [SynthRoot(context, e.mod_instrument, out[id(e)].synth_root, not stated, out[id(e)].shift, per,
                        pitches.get(id(e), {}))
              for context, e, per, _d, stated in items]
    return ResolvedRoots(report, out)
