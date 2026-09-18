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


class DriverState:
    """Mutable SMPS track state, advanced one coordination flag at a time."""

    __slots__ = ("att", "config", "hard_panned", "instrument", "is_psg",
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
            form_byte = effect.params[0]
            entry = self.config.psg_map.get(form_byte)
            if entry is not None:
                self.instrument = entry.mod_instrument
                self.psg_entry = entry
                self.psg_entries = None         # smpsPSGform is not a voice-map event
                self.psg_label = f"form {form_byte:#04x}"

        elif kind == 'smpsPSGvoice':
            label = effect.params[0]
            entries = self.config.psg_voice_map.get(label)
            # In noise mode (after smpsPSGform, or under a noise psg_voice_map entry) this
            # only changes the envelope: a noise entry under the label is that envelope's
            # variant (Scrap Brain's fTone_04 / fTone_08 instruments); a tone entry is
            # ignored and the channel stays on its noise instrument (Credits, where the
            # labels belong to PSG1/PSG2).  cfSetPSGNoise is permanent.
            if entries is not None and not (self.in_noise_mode and entries[0].type == "tone"):
                self.instrument = entries[0].mod_instrument
                self.psg_entry = entries[0]
                self.psg_entries = entries
                self.psg_label = label

    # -- queries -------------------------------------------------------------

    @property
    def in_noise_mode(self) -> bool:
        """True once a noise instrument is active; nothing in Sonic 1 music leaves it."""
        return self.psg_entry is not None and self.psg_entry.type != "tone"

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
