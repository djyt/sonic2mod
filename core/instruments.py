"""The instrument catalogue: which config entry each synthesised MOD instrument is rendered for.

A MOD instrument can be named by several map entries (Credits folds its voices onto 31
slots, Stage Clear's PSG2 sits two octaves up its PSG1 sample).  The sample is rendered
once, for the FIRST entry that names the instrument, in the order the maps are walked:

    FM:  voice_map (voices the song defines), channel_instrument_map (rooted entries),
         legacy_voice_map, channel_instrument_map (rootless entries, played at C1)
    PSG: psg_map (each entry's own instrument, then its `envelopes:` variants), psg_voice_map

Everything that needs to know "what is instrument N rendered as" reads it from here: the
sample generators (what to render), the converter's sustain scan (the rate each sample
plays at) and the sample installer.  A later entry that shares the instrument anchors
another range onto the same sample and says nothing about how it is rendered.

An FM instrument is a list of layers, each a voice at a pitch offset, an FNUM detune and a
carrier TL offset relative to the instrument's own.  A plain instrument has one layer at
offset 0; a composite (several SMPS channels folded onto one MOD channel) has one per
channel, rendered together on the chip exactly as the hardware sums them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .config import ConversionConfig, InstrumentRange, PsgInstrumentEntry
from .tables import PERIOD_TABLE, ModNote

# Legacy / rootless fallback: C4 rendered (renderer index 36, 261.6 Hz) and played at C1's
# rate, what an SMPS nC5 sounds like on a channel with the usual $F4 (-12) pitch offset.
STD_SYNTH_IDX = 36


@dataclass(slots=True)
class FmLayer:
    """One YM2612 channel of an FM instrument's render."""
    voice_idx: int
    semitones: int = 0       # this layer's pitch above the instrument's rendering pitch
    fnum_offset: int = 0     # raw FNUM detune added to the frequency word (smpsAlterNote)
    tl_offset: int = 0       # carrier TL relative to the instrument's render level


@dataclass(slots=True)
class FmInstrument:
    """How one MOD instrument's FM sample is rendered."""
    inst: int
    entry: InstrumentRange   # the entry the sample is rendered for (root / synth_root / synth_shift)
    layers: list[FmLayer]
    context: str             # "voice_map[0][1]", "channel_instrument_map[FM5][0][0]", ...
    source_label: str = ""   # the channel_instrument_map channel, "" for voice_map
    legacy: bool = False     # from the deprecated legacy_voice_map form

    @property
    def root_idx(self) -> int | None:
        """MOD note `root` (None for a rootless entry, whose sample plays at C1)."""
        return self.entry.root.value if self.entry.root is not None else None

    @property
    def rate_root_idx(self) -> int:
        """The MOD note whose playback rate the sample is made for."""
        return self.root_idx if self.root_idx is not None else ModNote.C1.value

    @property
    def synth_shift(self) -> int:
        """Semitones the sample is rendered above the pitch `root` sounds (resolve_synth_roots)."""
        return self.entry.synth_shift if self.root_idx is not None else 0

    @property
    def synth_idx(self) -> int:
        """Renderer note index (C1 = 0) the sample is rendered at."""
        if self.root_idx is None:
            return STD_SYNTH_IDX
        if self.entry.synth_root is not None:
            return self.entry.synth_root - 12
        return self.entry.low - 12

    def target_rate(self, amiga_clock: float) -> int:
        """The sample's rate: root's playback rate, raised by the synth_shift ratio."""
        base = amiga_clock / PERIOD_TABLE[self.rate_root_idx]
        return round(base * 2.0 ** (self.synth_shift / 12.0))


@dataclass(slots=True)
class FmCatalogue:
    instruments: dict[int, FmInstrument] = field(default_factory=dict)   # in render order
    # (context, voice index, instruments the entries name) for map entries whose voice the
    # song does not define; nothing is rendered for them
    missing_voices: list[tuple[str, int, list[int]]] = field(default_factory=list)


def fm_catalogue(song, config: ConversionConfig) -> FmCatalogue:
    """Every FM instrument the config has synthesised, each with the entry it is rendered for."""
    voices = {v.index for v in song.voices}
    cat = FmCatalogue()

    def add(entry: InstrumentRange, voice_idx: int, context: str, source_label: str = "",
            legacy: bool = False) -> None:
        if entry.mod_instrument in cat.instruments:
            return
        cat.instruments[entry.mod_instrument] = FmInstrument(
            entry.mod_instrument, entry, [FmLayer(voice_idx)], context, source_label, legacy)

    def rooted(voice_idx: int, ranges, context: str, source_label: str = "") -> None:
        if voice_idx not in voices:
            cat.missing_voices.append((context, voice_idx, [e.mod_instrument for e in ranges]))
            return
        for i, entry in enumerate(ranges):
            if entry.root is not None:
                add(entry, voice_idx, f"{context}[{i}]", source_label)

    for voice_idx, ranges in config.voice_map.items():
        rooted(voice_idx, ranges, f"voice_map[{voice_idx}]")
    for src, vim in config.channel_instrument_map.items():
        for voice_idx, ranges in vim.items():
            rooted(voice_idx, ranges, f"channel_instrument_map[{src}][{voice_idx}]", src)
    for voice_idx, inst in config.legacy_voice_map.items():
        if voice_idx in voices:
            add(InstrumentRange(low=60, high=60, mod_instrument=inst), voice_idx,
                f"legacy_voice_map[{voice_idx}]", "legacy_voice_map", legacy=True)
    for src, vim in config.channel_instrument_map.items():
        for voice_idx, ranges in vim.items():
            if voice_idx not in voices:
                continue
            for i, entry in enumerate(ranges):
                if entry.root is None:
                    add(entry, voice_idx, f"channel_instrument_map[{src}][{voice_idx}][{i}]", src)
    plan = getattr(config, "merge_plan", None)
    if plan is not None:                         # core.merge: the composites, rendered as layers
        for spec in plan.fm_instruments:
            cat.instruments.setdefault(spec.inst, spec)
    return cat


@dataclass(slots=True)
class PsgInstrument:
    """How one MOD instrument's PSG sample is rendered: `entry` names this instrument and,
    for a noise envelope variant, carries that variant's envelope."""
    inst: int
    entry: PsgInstrumentEntry
    context: str

    @property
    def root_idx(self) -> int:
        return self.entry.root.value

    def target_rate(self, amiga_clock: float) -> int:
        return round(amiga_clock / PERIOD_TABLE[self.root_idx] * 2.0 ** (self.entry.synth_shift / 12.0))


def psg_catalogue(config: ConversionConfig, noise_envelopes: dict | None = None) -> dict[int, PsgInstrument]:
    """Every PSG instrument the config has synthesised, in render order.

    `noise_envelopes` ({instrument: label} from the converter's _derive_noise_envelopes)
    supplies the envelope of each psg_map instrument and variant; without it the entry's own
    `envelope:` stands.
    """
    noise_envelopes = noise_envelopes or {}
    out: dict[int, PsgInstrument] = {}

    def add(entry: PsgInstrumentEntry, context: str) -> None:
        if entry.root is not None and entry.mod_instrument not in out:
            out[entry.mod_instrument] = PsgInstrument(entry.mod_instrument, entry, context)

    for form, entry in config.psg_map.items():
        variants = [(entry.mod_instrument, noise_envelopes.get(entry.mod_instrument, entry.envelope), "")]
        variants += [(inst, noise_envelopes.get(inst, label), f".envelopes[{label}]")
                     for label, inst in entry.envelopes.items()]
        for inst, envelope, suffix in variants:
            add(dataclasses.replace(entry, mod_instrument=inst, envelope=envelope),
                f"psg_map[{form:#04x}]{suffix}")
    for label, entries in config.psg_voice_map.items():
        for i, entry in enumerate(entries):
            add(entry, f"psg_voice_map[{label}][{i}]")
    return out
