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
from .mod_notes import PERIOD_TABLE, ModNote

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
    keyoff_secs: float | None = None   # key this layer off that long after key-on (a follower's
                                       # smpsNoteFill in a composite); None: with the instrument


@dataclass(slots=True)
class FmInstrument:
    """How one MOD instrument's FM sample is rendered."""
    inst: int
    entry: InstrumentRange   # the entry the sample is rendered for (root / synth_root / synth_shift)
    layers: list[FmLayer]
    context: str             # "voice_map[0][1]", "channel_instrument_map[FM5][0][0]", ...
    source_label: str = ""   # the channel_instrument_map channel, "" for voice_map
    legacy: bool = False     # from the deprecated legacy_voice_map form
    loop_drift_db: float | None = None   # a merge group's overrides for its composite; else the entry's
    loop_min_ms: float | None = None
    treble_shelf_db: float | None = None   # a merge group's shelf on this composite's render
    treble_shelf_hz: float | None = None
    dither: str | None = None              # a merge group's quantisation for this composite

    @property
    def drift_db(self) -> float | None:
        """This instrument's sustain loop drift override (None: the song's)."""
        return self.loop_drift_db if self.loop_drift_db is not None else self.entry.loop_drift_db

    @property
    def dither_mode(self) -> str | None:
        """This sample's quantisation override (None: the settings')."""
        return self.dither if self.dither is not None else self.entry.dither

    @property
    def min_loop_ms(self) -> float | None:
        """This instrument's shortest sustain loop override (None: core.loops' default)."""
        return self.loop_min_ms if self.loop_min_ms is not None else self.entry.loop_min_ms

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
    _add_detune_variants(cat, getattr(config, "detune_plan", None))
    plan = getattr(config, "merge_plan", None)
    if plan is not None:                         # core.merge: the composites, rendered as layers
        for inst in plan.dropped:                # nothing the merged build plays or mixes ...
            cat.instruments.pop(inst, None)
        for spec in plan.fm_instruments:         # ... and a composite owns its slot (the plan never
            cat.instruments[spec.inst] = spec    # reuses an FM mix source's)
    return cat


def _add_detune_variants(cat: FmCatalogue, plan) -> None:
    """core.detune: each instrument's sample rendered at its own detune, and a copy of it in
    every variant's slot at the variant's."""
    if plan is None:
        return
    for v in plan.variants.values():
        base = cat.instruments.get(v.base)
        if base is not None:
            cat.instruments[v.inst] = dataclasses.replace(
                base, inst=v.inst, layers=[dataclasses.replace(base.layers[0], fnum_offset=v.detune)],
                context=f"{base.context} detune {v.detune:+d}")
    for inst, detune in plan.own.items():
        base = cat.instruments.get(inst)
        if base is not None:
            base.layers = [dataclasses.replace(base.layers[0], fnum_offset=detune)]


def free_slots(config, song) -> list[int]:
    """Instrument slots nothing in the config names."""
    used = {e[0] for e in (config.sample_list or [])}
    used |= set(fm_catalogue(song, config).instruments)
    used |= set(psg_catalogue(config))
    used |= {d.mod_instrument for d in config.dac_samples}
    return [i for i in range(1, 32) if i not in used]


@dataclass(slots=True)
class PsgInstrument:
    """How one MOD instrument's PSG sample is rendered: `entry` names this instrument and,
    for a noise envelope variant, carries that variant's envelope."""
    inst: int
    entry: PsgInstrumentEntry
    context: str
    source: str = ""         # the smpsPSGvoice label (tone) or the smpsPSGform byte, "$E7" (noise)

    @property
    def root_idx(self) -> int:
        return self.entry.root.value

    def target_rate(self, amiga_clock: float) -> int:
        return round(amiga_clock / PERIOD_TABLE[self.root_idx] * 2.0 ** (self.entry.synth_shift / 12.0))


def psg_catalogue(config: ConversionConfig, noise_envelopes: dict | None = None) -> dict[int, PsgInstrument]:
    """Every PSG instrument the config has synthesised, in render order.

    `noise_envelopes` ({instrument: label} from the converter's derive_noise_envelopes)
    supplies the envelope of each psg_map instrument and variant; without it the entry's own
    `envelope:` stands.
    """
    noise_envelopes = noise_envelopes or {}
    out: dict[int, PsgInstrument] = {}

    def add(entry: PsgInstrumentEntry, context: str, source: str) -> None:
        if entry.root is not None and entry.mod_instrument not in out:
            out[entry.mod_instrument] = PsgInstrument(entry.mod_instrument, entry, context, source)

    for form, entry in config.psg_map.items():
        variants = [(entry.mod_instrument, noise_envelopes.get(entry.mod_instrument, entry.envelope), "")]
        variants += [(inst, noise_envelopes.get(inst, label), f".envelopes[{label}]")
                     for label, inst in entry.envelopes.items()]
        for inst, envelope, suffix in variants:
            add(dataclasses.replace(entry, mod_instrument=inst, envelope=envelope),
                f"psg_map[{form:#04x}]{suffix}", f"${form:02X}")
    for label, entries in config.psg_voice_map.items():
        for i, entry in enumerate(entries):
            add(entry, f"psg_voice_map[{label}][{i}]", label)
    plan = getattr(config, "merge_plan", None)
    if plan is not None:
        # core.merge: nothing the merged build plays or mixes, and no slot a composite took over -
        # except a mix source's, which is rendered for the mixer and kept out of the slot
        for inst in plan.dropped | (plan.instruments - plan.mix_only):
            out.pop(inst, None)
    return out
