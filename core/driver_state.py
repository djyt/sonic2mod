"""The SMPS track state that decides an event's pitch, level and instrument.

Four passes over a channel's events used to each re-implement this state machine:
the two "baked" level pre-passes, the rate-3 divider derivation and the conversion
itself, plus the analyser and two config-generating tools.  They had already drifted
(the rule for what a `smpsPSGvoice` may do once the channel is in noise mode was
written three different ways), so it lives here once.

    for chan_cfg, channel in enabled_channels(song, config, ("FM", "PSG")):
        for event, st, res in walk_channel(channel, config, chan_cfg):
            if res is not None:                 # a pitched note: res.instrument, res.index ...
                ...

`walk_channel` advances a `DriverState` through the channel's events and hands every
pitched note to `resolve_note`, the one place that says which MOD instrument a note is
routed to and which MOD note it triggers (`ResolvedNote`).  The conversion, its level and
sustain pre-passes, the noise / rate-3 derivations and `resolve_synth_roots` all read that
one answer.

`DriverState` tracks only what every caller needs: the hardware level, the pan, the
driver transpose, the detune, the current FM voice and the active PSG instrument entry.  State
that is only meaningful while emitting MOD data (note fill, vibrato, cursor) stays
in the converter.
"""

from __future__ import annotations

from dataclasses import dataclass

from .driver_tables import psg_index_semitone
from .levels import FM_TL_SILENT, PSG_ATT_SILENT, fm_level_db, psg_level_db

# --- source names -----------------------------------------------------------


def source_names(song) -> list[str]:
    """"DAC", "FM1".."FMn", "PSG1".."PSGn" for a parsed song's channels, in header order."""
    names: list[str] = []
    fm = psg = 0
    for ch in song.channels:
        kind = ch.header.channel_type
        if kind == "DAC":
            names.append("DAC")
        elif kind == "FM":
            fm += 1
            names.append(f"FM{fm}")
        else:
            psg += 1
            names.append(f"PSG{psg}")
    return names


def source_map(song) -> dict:
    """Source name -> parsed channel, in header order."""
    return dict(zip(source_names(song), song.channels, strict=True))


def chip_pitch(source_semitone: int, transpose: int, is_psg: bool) -> int:
    """The real pitch (SMPS semitone, C0 = 0) the chip plays for a note byte and transpose.

    `transpose` is the driver's: the header pitch_offset plus every smpsChangeTransposition.
    A PSG note goes through the driver's frequency table, so one transposed past either end
    of it lands on whatever the hardware reads there.
    """
    return (psg_index_semitone(source_semitone + transpose) if is_psg
            else source_semitone + transpose)


def pan_is_hard(params: list) -> bool:
    """True for smpsPan panLeft / panRight (params arrive as one 'panLeft, $00' string)."""
    direction = str(params[0]).split(',')[0].strip().lower() if params else ''
    return direction in ('panleft', 'panright')


def psg_range_entry(entries, key: int):
    """Entry of a multi-entry psg_voice_map list whose low/high bracket covers `key`, or None."""
    if entries is None or len(entries) <= 1:
        return None
    for e in entries:
        lo = e.low if e.low is not None else 0        # 0 = C0 (semitone floor)
        hi = e.high if e.high is not None else 255    # 255 > B7 (~95), matches all
        if lo <= key <= hi:
            return e
    return None


# --- the state machine ------------------------------------------------------


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
            shift = max(0, min(max(counts, key=lambda c: (counts[c], -c)) - d_first, 12))
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


class DriverState:
    """Mutable SMPS track state, advanced one coordination flag at a time."""

    __slots__ = ("att", "config", "detune", "envelope", "hard_panned", "instrument", "is_psg",
                 "noise_form", "psg_entries", "psg_entry", "psg_label", "tl", "transpose", "voice")

    def __init__(self, config, *, is_psg: bool, transpose: int = 0,
                 volume: int = 0, instrument: int = 0):
        self.config = config
        self.is_psg = is_psg
        self.transpose = transpose          # header pitch_offset + every smpsChangeTransposition
        self.tl = 0 if is_psg else volume   # YM2612 TL offset, 0-127
        self.att = volume if is_psg else 0  # SN76489 attenuation, 0-15
        self.hard_panned = False
        self.detune = 0                     # smpsDetune / smpsAlterNote: raw FNUM (PSG: divider) offset
        self.voice: int | None = None       # smpsSetvoice index
        self.instrument = instrument        # MOD instrument slot currently routed to
        self.psg_entry = None               # active PsgInstrumentEntry
        self.psg_entries = None             # its full psg_voice_map list, if it came from one
        self.psg_label: str | None = None   # "fTone_01" / "form 0xe7", for warnings
        self.envelope: str | None = None    # the driver's VoiceIndex: header voice, then every smpsPSGvoice
        self.noise_form: int | None = None  # the smpsPSGform byte once one ran (SMPS_Track.PSGNoise); permanent

    @classmethod
    def for_channel(cls, channel, config, instrument: int = 0) -> DriverState:
        """Initial state for a parsed channel: header transpose, volume and PSG voice."""
        header = channel.header
        st = cls(config,
                 is_psg=header.channel_type == "PSG",
                 transpose=header.pitch_offset,
                 volume=header.volume,
                 instrument=instrument)
        label = header.psg_voice_label
        st.envelope = label or None
        entries = config.psg_voice_map.get(label) if label else None
        if entries:
            st.instrument = entries[0].mod_instrument
            st.psg_entry = entries[0]
            st.psg_entries = entries
            st.psg_label = label
        return st

    # -- advancing -----------------------------------------------------------

    def apply(self, effect) -> None:
        """Advance the state for one coordination flag.  Unknown flags are ignored."""
        kind = effect.effect_type

        if kind == 'smpsSetvoice':
            self.voice = effect.params[0]
            self.instrument = self.config.legacy_voice_map.get(self.voice, self.instrument)

        elif kind == 'smpsAlterVol':
            delta = effect.params[0]
            if self.is_psg:
                self.att = max(0, min(PSG_ATT_SILENT, self.att + delta))
            else:
                self.tl = max(0, min(FM_TL_SILENT, self.tl + delta))

        elif kind == 'smpsPan':
            self.hard_panned = pan_is_hard(effect.params)

        elif kind == 'smpsAlterNote':
            # SMPS_Track.Detune: added to the frequency word the driver writes (about 10 cents
            # per unit on FM).  Not a semitone: it never moves a note or a range lookup; it is
            # what a chorus pair's beating and a composite layer's FNUM offset come from.
            self.detune = effect.params[0]

        elif kind == 'smpsChangeTransposition':
            self.transpose += effect.params[0]

        elif kind == 'smpsPSGform':
            # cfSetPSGNoise: the channel is a noise channel from here on (nothing in Sonic 1
            # music turns it back) and the form byte says white/periodic and the rate.  The
            # envelope is whatever VoiceIndex holds — the header voice or the last smpsPSGvoice.
            form_byte = effect.params[0]
            self.noise_form = form_byte
            entry = self.config.psg_map.get(form_byte)
            if entry is not None:
                self.psg_entry = entry
                self.psg_entries = None         # smpsPSGform is not a voice-map event
                self.psg_label = f"form {form_byte:#04x}"
                self.instrument = entry.envelopes.get(self.envelope, entry.mod_instrument)

        elif kind == 'smpsPSGvoice':
            # cfSetPSGTone: VoiceIndex changes whatever mode the channel is in.  In noise mode
            # that only changes the envelope the noise plays with: the instrument stays the
            # psg_map entry's, or the variant its `envelopes:` names for this label (Scrap
            # Brain's fTone_08 hi-hat); psg_voice_map is not consulted (Credits' labels belong
            # to PSG1/PSG2).  In tone mode the label picks the psg_voice_map instrument.
            label = effect.params[0]
            self.envelope = label
            if self.in_noise_mode:
                if self.psg_entry is not None:
                    self.instrument = self.psg_entry.envelopes.get(label, self.psg_entry.mod_instrument)
            else:
                entries = self.config.psg_voice_map.get(label)
                if entries is not None:
                    self.instrument = entries[0].mod_instrument
                    self.psg_entry = entries[0]
                    self.psg_entries = entries
                    self.psg_label = label

    # -- queries -------------------------------------------------------------

    @property
    def in_noise_mode(self) -> bool:
        """True once smpsPSGform ran on this channel; nothing in Sonic 1 music leaves it."""
        return self.noise_form is not None

    def range_key(self, source_semitone: int, range_space: str | None = None) -> int:
        """What a note is matched against voice_map / psg_voice_map ranges with.

        The source byte (`range_space: source`) or the chip's real pitch
        (`range_space: chip` = byte + pitch_offset + smpsChangeTransposition; PSG through
        the driver's frequency table, so notes past its ends sound as the hardware does).
        """
        space = self.config.range_space if range_space is None else range_space
        if space != "chip":
            return source_semitone
        return chip_pitch(source_semitone, self.transpose, self.is_psg)

    def fm_ranges(self, source: str):
        """The range list the current voice routes through on this channel: its
        channel_instrument_map list, else its voice_map list, else None."""
        return (self.config.channel_instrument_map.get(source, {}).get(self.voice)
                or self.config.voice_map.get(self.voice))

    def fm_range_entry(self, source: str, key: int):
        """voice_map / channel_instrument_map entry covering this note, or None."""
        for entry in self.fm_ranges(source) or ():
            if entry.low <= key <= entry.high:
                return entry
        return None

    def psg_ranged_entry(self, key: int):
        """The psg_voice_map list entry covering this note, or the active entry."""
        return psg_range_entry(self.psg_entries, key) or self.psg_entry

    def level_db(self, pan_law_db: float) -> float:
        """Hardware level of a note played right now, relative to full scale."""
        if self.is_psg:
            return psg_level_db(self.att)
        return fm_level_db(self.tl, self.hard_panned, pan_law_db)

    @property
    def is_silent(self) -> bool:
        """True when the track's own volume puts a note below audibility."""
        return self.att >= PSG_ATT_SILENT if self.is_psg else self.tl >= FM_TL_SILENT


# --- resolving a note ---------------------------------------------------------


@dataclass(slots=True)
class ResolvedNote:
    """What one SMPS note plays in the MOD: which instrument, at which MOD note.

    `path` says how the MOD note was found:
      "fm_root"   - an FM voice_map / channel_instrument_map entry with `root`:
                    root + (key - low)
      "psg_root"  - a PSG entry with `root` and `low`: the same formula
      "psg_fixed" - a noise entry with `root`: always root (its note bytes carry no pitch)
      "transpose" - no anchor: source + driver transpose + the channel config's transpose
                    (a rootless entry still names the instrument)
    `index` is clamped to the MOD's three octaves; `raw_index` is not.
    """
    instrument: int          # MOD instrument slot
    index: int               # MOD note index, 0 = C1 .. 35 = B3
    raw_index: int           # the same before clamping
    path: str
    entry: object | None     # the InstrumentRange / PsgInstrumentEntry that routed it, or None
    source: int              # source semitone (note byte - $81)
    key: int                 # what the ranges were matched against (DriverState.range_key)
    chip: int                # the real pitch the chip plays (chip_pitch)
    total_transpose: int     # driver transpose + the channel config's transpose
    detune: int = 0          # the track's smpsDetune in force (raw FNUM / divider units)

    @property
    def clamped(self) -> bool:
        return self.index != self.raw_index

    @property
    def anchored(self) -> bool:
        """True when a `root` placed the note (the transpose path did not)."""
        return self.path != "transpose"


def resolve_note(st: DriverState, source_semitone: int, chan_transpose: int, source: str) -> ResolvedNote:
    """Which MOD instrument and MOD note a pitched note plays, given the track state.

    FM: the current voice's range entry covering the note (channel_instrument_map first,
    then voice_map) names the instrument; with `root` it also places the note.  PSG: a
    multi-entry psg_voice_map list dispatches per note on `low`/`high` (and that entry
    stays active, as the driver keeps a voice), otherwise the active entry; `root` with
    `low` places a tone, `root` alone places noise, and a rooted tone without `low` falls
    to the transpose path.  Noise mode's instrument is the state's own (the envelope
    variant smpsPSGform / smpsPSGvoice chose).
    """
    key = st.range_key(source_semitone)
    total = st.transpose + chan_transpose
    chip = chip_pitch(source_semitone, st.transpose, st.is_psg)
    inst, raw, path, entry = st.instrument, None, "transpose", None
    if not st.is_psg:
        entry = st.fm_range_entry(source, key)
        if entry is not None:
            inst = entry.mod_instrument
            if entry.root is not None:
                raw, path = entry.root.value + (key - entry.low), "fm_root"
    else:
        ranged = psg_range_entry(st.psg_entries, key)
        if ranged is not None:
            st.psg_entry = ranged
            inst = ranged.mod_instrument
        entry = st.psg_entry
        if entry is not None and entry.root is not None:
            if entry.low is not None:
                raw, path = entry.root.value + (key - entry.low), "psg_root"
            elif entry.type != "tone":
                raw, path = entry.root.value, "psg_fixed"
    if raw is None:
        raw = source_semitone + total
    return ResolvedNote(inst, max(0, min(35, raw)), raw, path, entry,
                        source_semitone, key, chip, total, st.detune)


def walk_channel(channel, config, chan_cfg, st: DriverState | None = None):
    """Yield (event, state, resolved) for every event of a channel, in order.

    The state is advanced past each coordination flag before the flag is yielded, and every
    pitched note (not a rest, not a DAC hit) comes with its `ResolvedNote`; the other events
    come with None.  The same `DriverState` object is yielded every time - read it as you go.
    Pass `st` to start from a state you set up yourself.  With a merge plan on the config
    (`convert.py --merged`), a note that plays a composite instrument is resolved to it.
    """
    if st is None:
        st = DriverState.for_channel(channel, config, chan_cfg.instrument)
    plan = getattr(config, "merge_plan", None)       # core.merge: composite instruments per tick
    for event in channel.events:
        res = None
        if event.is_effect:
            st.apply(event.effect)
        elif event.is_note and not event.note.is_rest and not event.note.is_dac:
            res = resolve_note(st, event.note.note_value - 0x81, chan_cfg.transpose, chan_cfg.source)
            if plan is not None:
                res.instrument = plan.instrument_at(chan_cfg.source, event.tick_position, res.instrument)
        yield event, st, res


def enabled_channels(song, config, kinds=("FM", "PSG")):
    """(chan_cfg, parsed channel) for every enabled config channel of the given chip kinds."""
    smap = source_map(song)
    for chan_cfg in config.channels:
        channel = smap.get(chan_cfg.source)
        if chan_cfg.enabled and channel is not None and channel.header.channel_type in kinds:
            yield chan_cfg, channel
