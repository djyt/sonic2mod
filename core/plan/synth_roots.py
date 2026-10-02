"""The pitch each rooted map entry's sample is rendered at, from the song."""

from __future__ import annotations

from .driver_state import enabled_channels, walk_channel


def resolve_synth_roots(song, config) -> list[dict]:
    """Fill in every rooted map entry's `synth_root` from the song, and its `synth_shift`.

    A sample is rendered at synth_root and played at root's rate, so MOD note m sounds at
    synth_root + (m − root).  A source note is placed at m = root + (key − low) (an anchored
    entry) or at its channel transpose (a rooted PSG entry without `low`), and the chip plays
    it at its real pitch (chip_pitch).  For the note to be in tune the sample must be rendered
    at D = chip pitch − (m − root): for an anchored entry that is the pitch the chip plays for
    `low`.  This walks every enabled FM/PSG channel with the DriverState, collects D for each
    note an entry routes, and

    - where the config leaves synth_root out, renders at the chip pitch the entry's notes play
      most often (ties to the lower one), at most an octave above D, and sets
      `synth_shift` = that pitch − D.  Envelopes run in real time on the chip but stretch
      with playback rate in a MOD, so rendering at the busiest note keeps the most notes'
      attack and decay at the hardware's speed.  A config never needs to state it.
    - sets `synth_shift = synth_root − D` where it is stated (a rendering pitch anywhere in
      the range).

    The shift never moves a note: the sample generators give the sample a rate 2^(shift/12)
    higher than `root`'s playback rate, so MOD note `root` still sounds D and every note keeps
    its place, its playback rate and its bandwidth (placing notes lower instead halved the
    Chaos Emerald lead's rate to 4 kHz).  The cost is the sample's size, 2^(shift/12) times —
    hence the octave cap.

    Returns one dict per entry: {'context', 'instrument', 'synth_root', 'derived', 'shift',
    'votes': {D: notes}, 'pitches': {chip pitch: notes}}.  More than one D means the entry's
    low is played at several chip pitches (several pitch offsets or an
    smpsChangeTransposition under one source range — what `range_space: chip` and a split
    entry are for); the caller warns.
    """
    votes: dict[int, dict[int, int]] = {}       # id(entry) -> {D: notes}
    pitches: dict[int, dict[int, int]] = {}     # id(entry) -> {chip pitch: notes}
    for chan_cfg, channel in enabled_channels(song, config, ("FM", "PSG")):
        for _event, st, res in walk_channel(channel, config, chan_cfg):
            if res is None:
                continue
            entry = res.entry
            if entry is None or entry.root is None:
                continue
            if st.is_psg and (st.in_noise_mode or entry.type != "tone"):
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
    cap = 12
    if getattr(config, "merge_active", False):
        cap = max(0, min(12, int(getattr(config, "merge_max_synth_shift", 12))))
    groups: dict[int, list] = {}
    for item in items:
        groups.setdefault(item[1].mod_instrument, []).append(item)
    for group in groups.values():
        _, first, _, d_first, first_stated = group[0]
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
                e.synth_shift = (e.synth_root - d) if d is not None else 0   # its own say, as before
            else:
                e.synth_root = pitch
                e.synth_shift = shift if d is not None else 0

    out = []
    for context, e, per, _d, stated in items:
        out.append({'context': context, 'instrument': e.mod_instrument, 'synth_root': e.synth_root,
                    'derived': not stated, 'shift': e.synth_shift, 'votes': per,
                    'pitches': pitches.get(id(e), {})})
    return out
