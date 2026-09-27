"""The SMPS track state that decides an event's pitch, level and instrument.

Four passes over a channel's events used to each re-implement this state machine:
the two "baked" level pre-passes, the rate-3 divider derivation and the conversion
itself, plus the analyser and two config-generating tools.  They had already drifted
(the rule for what a `smpsPSGvoice` may do once the channel is in noise mode was
written three different ways), so it lives here once.

    st = DriverState.for_channel(channel, config, chan_cfg.instrument)
    for event in channel.events:
        if event.is_effect:
            st.apply(event.effect)
        elif event.is_note and not event.note.is_rest:
            key = st.range_key(event.note.note_value - 0x81)
            entry = st.psg_ranged_entry(key) if st.is_psg else st.fm_range_entry(source, key)
            inst = entry.mod_instrument if entry is not None else st.instrument

`DriverState` tracks only what every caller needs: the hardware level, the pan, the
driver transpose, the current FM voice and the active PSG instrument entry.  State
that is only meaningful while emitting MOD data (note fill, vibrato, cursor) stays
in the converter.
"""

from __future__ import annotations

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
      most often (ties to the lower one: a sample played below its rendering pitch runs
      slower and must be longer) and sets `synth_shift` = that pitch − D, capped so the
      lowest note the entry places still lands on C1.  Envelopes run in real time on the
      chip but stretch with playback rate in a MOD, so rendering at the busiest note keeps
      the most notes' attack and decay at the hardware's speed.  A config never needs to
      state it.
    - sets `synth_shift = synth_root − D` where it is stated, so a sample rendered anywhere in
      its range is placed lower by that much and stays in tune: the conversion subtracts
      synth_shift from m.

    Returns one dict per entry: {'context', 'instrument', 'synth_root', 'derived', 'shift',
    'votes': {D: notes}, 'pitches': {chip pitch: notes}}.  More than one D means the entry's
    low is played at several chip pitches (several pitch offsets or an
    smpsChangeTransposition under one source range — what `range_space: chip` and a split
    entry are for); the caller warns.
    """
    votes: dict[int, dict[int, int]] = {}       # id(entry) -> {D: notes}
    pitches: dict[int, dict[int, int]] = {}     # id(entry) -> {chip pitch: notes}
    lowest: dict[int, int] = {}                 # id(entry) -> smallest m − root it places
    highest: dict[int, int] = {}                # id(entry) -> largest m − root it places
    smap = source_map(song)
    for chan_cfg in config.channels:
        channel = smap.get(chan_cfg.source)
        if not chan_cfg.enabled or channel is None or channel.header.channel_type not in ("FM", "PSG"):
            continue
        st = DriverState.for_channel(channel, config, chan_cfg.instrument)
        for event in channel.events:
            if event.is_effect:
                st.apply(event.effect)
                continue
            if not event.is_note or event.note.is_rest or getattr(event.note, "is_dac", False):
                continue
            src = event.note.note_value - 0x81
            key = st.range_key(src)
            real = chip_pitch(src, st.transpose, st.is_psg)
            if st.is_psg:
                if st.in_noise_mode:
                    continue
                ranged = psg_range_entry(st.psg_entries, key)
                if ranged is not None:
                    st.psg_entry = ranged
                entry = st.psg_entry
                if entry is None or entry.root is None or entry.type != "tone":
                    continue
                if entry.low is not None:
                    m_rel = key - entry.low                      # m − root, anchored
                else:
                    m_rel = src + st.transpose + chan_cfg.transpose - entry.root.value
            else:
                entry = st.fm_range_entry(chan_cfg.source, key)
                if entry is None or entry.root is None:
                    continue
                m_rel = key - entry.low
            per = votes.setdefault(id(entry), {})
            per[real - m_rel] = per.get(real - m_rel, 0) + 1
            pp = pitches.setdefault(id(entry), {})
            pp[real] = pp.get(real, 0) + 1
            rel = int(m_rel)
            lowest[id(entry)] = min(lowest.get(id(entry), rel), rel)
            highest[id(entry)] = max(highest.get(id(entry), rel), rel)

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
    # D says nothing about the sample.  Moving the rendering pitch by s semitones therefore
    # moves every entry's placement by the same s: the chip pitch their notes play most often
    # sets s, held within the window where the lowest note any of them places still lands on
    # C1 and, when both fit, the highest on B3.
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
            floor_cap = 10 ** 6
            ceil_need = -10 ** 6
            for _, e, _, d, _ in group:
                if d is None:
                    continue
                for chip, n in pitches.get(id(e), {}).items():
                    counts[chip] = counts.get(chip, 0) + n
                floor_cap = min(floor_cap, e.root.value + lowest[id(e)])
                ceil_need = max(ceil_need, e.root.value + highest[id(e)] - 35)
            shift = min(max(counts, key=lambda c: (counts[c], -c)) - d_first, floor_cap)
            if ceil_need <= floor_cap:
                shift = max(shift, ceil_need)
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

    __slots__ = ("att", "config", "envelope", "hard_panned", "instrument", "is_psg", "noise_form",
                 "psg_entries", "psg_entry", "psg_label", "tl", "transpose", "voice")

    def __init__(self, config, *, is_psg: bool, transpose: int = 0,
                 volume: int = 0, instrument: int = 0):
        self.config = config
        self.is_psg = is_psg
        self.transpose = transpose          # header pitch_offset + every smpsChangeTransposition
        self.tl = 0 if is_psg else volume   # YM2612 TL offset, 0-127
        self.att = volume if is_psg else 0  # SN76489 attenuation, 0-15
        self.hard_panned = False
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

    def fm_range_entry(self, source: str, key: int):
        """voice_map / channel_instrument_map entry covering this note, or None."""
        ranges = (self.config.channel_instrument_map.get(source, {}).get(self.voice)
                  or self.config.voice_map.get(self.voice))
        for entry in ranges or ():
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
