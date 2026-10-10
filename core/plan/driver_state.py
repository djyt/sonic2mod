"""The SMPS track state that decides an event's pitch, level and instrument.

Four passes over a channel's events used to each re-implement this state machine:
the two "baked" level pre-passes, the rate-3 divider derivation and the conversion
itself, plus the analyser and two config-generating tools.  They had already drifted
(the rule for what a `smpsPSGvoice` may do once the channel is in noise mode was
written three different ways), so it lives here once.

    for chan_cfg, channel in enabled_channels(song, config, (ChannelType.FM, ChannelType.PSG)):
        for event, st, res in walk_channel(channel, config, chan_cfg):
            if res is not None:                 # a pitched note: res.instrument, res.index ...
                ...

`walk_channel` advances a `DriverState` through the channel's events and hands every
pitched note to `resolve_note`, the one place that says which MOD instrument a note is
routed to and which MOD note it triggers (`ResolvedNote`).  The conversion, its level and
sustain pre-passes, the noise / rate-3 derivations and `resolve_synth_roots` all read that
one answer.

`DriverState` is the driver's track state (core.smps.TrackState: level, pan, transpose,
detune, FM voice, envelope, noise form) plus the MOD routing it decides: the instrument and the
active PSG entry.  State that is only meaningful while emitting MOD data (cursor, vibrato
placement) stays in the converter.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import (
    ChannelType,
    PsgForm,
    PsgVoice,
    TrackState,
    chip_pitch,
    source_map,
)


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


class DriverState(TrackState):
    """The driver's track state plus the MOD instrument it routes to, advanced one coordination
    flag at a time."""

    __slots__ = ("config", "instrument", "psg_entries", "psg_entry", "psg_label")

    def __init__(self, config, *, is_psg: bool, psg_read: tuple[int, ...], transpose: int = 0,
                 volume: int = 0, instrument: int = 0):
        super().__init__(is_psg=is_psg, psg_read=psg_read, transpose=transpose, volume=volume)
        self.config = config
        self.instrument = instrument        # MOD instrument slot currently routed to
        self.psg_entry = None               # active PsgInstrumentEntry
        self.psg_entries = None             # its full psg_voice_map list, if it came from one
        self.psg_label: str | None = None   # "fTone_01" / "form 0xe7", for warnings

    @classmethod
    def for_channel(cls, channel, config, instrument: int = 0) -> DriverState:
        """Initial state for a parsed channel: header transpose, volume and PSG voice."""
        header = channel.header
        st = cls(config,
                 is_psg=header.channel_type == ChannelType.PSG,
                 psg_read=channel.rules.psg_read,
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
        """Advance the track state for one coordination flag, then the MOD routing it decides."""
        super().apply(effect)

        if isinstance(effect, PsgForm):
            # The form byte says white/periodic and the rate: its psg_map entry plays the noise.
            # The envelope is whatever VoiceIndex holds — the header voice or the last smpsPSGvoice.
            form_byte = effect.noise
            entry = self.config.psg_map.get(form_byte)
            if entry is not None:
                self.psg_entry = entry
                self.psg_entries = None         # smpsPSGform is not a voice-map event
                self.psg_label = f"form {form_byte:#04x}"
                self.instrument = entry.envelopes.get(self.envelope, entry.mod_instrument)

        elif isinstance(effect, PsgVoice):
            # cfSetPSGTone: VoiceIndex changes whatever mode the channel is in.  In noise mode
            # that only changes the envelope the noise plays with: the instrument stays the
            # psg_map entry's, or the variant its `envelopes:` names for this label (Scrap
            # Brain's fTone_08 hi-hat); psg_voice_map is not consulted (Credits' labels belong
            # to PSG1/PSG2).  In tone mode the label picks the psg_voice_map instrument.
            label = effect.envelope
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

    def range_key(self, source_semitone: int, range_space: str | None = None) -> int:
        """What a note is matched against voice_map / psg_voice_map ranges with.

        The source byte (`range_space: source`) or the chip's real pitch
        (`range_space: chip` = byte + pitch_offset + smpsChangeTransposition; PSG through
        the driver's frequency table, so notes past its ends sound as the hardware does).
        """
        space = self.config.range_space if range_space is None else range_space
        if space != "chip":
            return source_semitone
        return chip_pitch(source_semitone, self.transpose, self.is_psg, self.psg_read)

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
      "merged"    - a follower's solo note on a merged channel (core.merge): resolved on the
                    follower's channel, carried over as it was
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
    gain_db: float = 0.0     # merged build: dB a unison chord folded into this note adds to its
                             #   level (core.merge.unison_gain_db); the note's level is the
                             #   track's plus this

    @property
    def clamped(self) -> bool:
        return self.index != self.raw_index

def resolve_note(st: DriverState, source_semitone: int, chan_transpose: int, source: str) -> ResolvedNote:
    """Which MOD instrument and MOD note a pitched note plays, given the track state.

    FM: the current voice's range entry covering the note (channel_instrument_map first,
    then voice_map) names the instrument; with `root` it also places the note.  PSG: a
    multi-entry psg_voice_map list dispatches per note on `low`/`high` (and that entry
    stays active, as the driver keeps a voice), otherwise the active entry; `root` with
    `low` places a tone, `root` alone places noise, and a rooted tone without `low` falls
    to the transpose path.  Noise mode's instrument is the state's own (the envelope
    variant smpsPSGform / smpsPSGvoice chose).  An FM note at a detune its instrument's sample
    is not rendered at plays that detune's variant (core.plan.detune).
    """
    key = st.range_key(source_semitone)
    total = st.transpose + chan_transpose
    chip = chip_pitch(source_semitone, st.transpose, st.is_psg, st.psg_read)
    inst, raw, path, entry = st.instrument, None, "transpose", None
    if not st.is_psg:
        entry = st.fm_range_entry(source, key)
        if entry is not None:
            inst = entry.mod_instrument
            if entry.root is not None:
                raw, path = entry.root.value + (key - entry.low), "fm_root"
        detune = getattr(st.config, "detune_plan", None)     # core.plan.detune: the sample at this detune
        if detune is not None:
            inst = detune.instrument_for(inst, st.detune, chip)
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
        solo = getattr(event, "merged", None)
        if solo is not None:
            # A follower's note spliced into this channel while the primary is silent
            # (core.merge): it plays the follower's instrument at the follower's level, so the
            # follower's state at that note stands in for this channel's.
            if not event.note.is_rest:
                res = ResolvedNote(solo.instrument, solo.index, solo.index, "merged", None,
                                   solo.note_value - 0x81, solo.index, solo.chip or 0, 0, solo.detune)
            yield event, (solo.state or st), res
            continue
        if event.is_effect:
            st.apply(event.effect)
        elif event.is_note and not event.note.is_rest and not event.note.is_dac:
            res = resolve_note(st, event.note.note_value - 0x81, chan_cfg.transpose, chan_cfg.source)
            if plan is not None:
                if (chan_cfg.source, event.tick_position) in plan.spliced:
                    res = None          # this note now plays on its primary's channel (a solo note)
                else:
                    res.instrument = plan.instrument_at(chan_cfg.source, event.tick_position, res.instrument)
                    res.gain_db = plan.gain_at(chan_cfg.source, event.tick_position)
        yield event, st, res


def enabled_channels(song, config, kinds=(ChannelType.FM, ChannelType.PSG)):
    """(chan_cfg, parsed channel) for every enabled config channel of the given chip kinds.

    In the merged build (`convert.py --merged`) the followers and the dropped channels are
    disabled in the output but still walked here: their notes vote for their instruments'
    levels, envelopes and rendering pitches, which the composites and the solo notes are made
    from, and the sample_list volumes were measured with those votes.
    """
    smap = source_map(song)
    followers = ({f for g in config.merge for f in g.followers} | set(config.merge_drop)
                 | set(getattr(config, "merge_fill", ()))
                 if getattr(config, "merge_active", False) else set())
    for chan_cfg in config.channels:
        channel = smap.get(chan_cfg.source)
        live = chan_cfg.enabled or chan_cfg.source in followers
        if live and channel is not None and channel.header.channel_type in kinds:
            yield chan_cfg, channel
