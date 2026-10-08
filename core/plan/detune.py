"""Detune variants: an smpsAlterNote / smpsDetune note plays a sample rendered with its FNUM offset.

The driver adds the track's Detune to the frequency word it writes (FMUpdateFreq), so a
detuned note is a few cents off the note table: Title Screen's FM5 `$03` is +5…8 c over FM1
(the pair beats), Scrap Brain's FM4 scoops `$EC` -49 c into a note.  A MOD can only retune a
whole sample (finetune, 12.5 c steps), so each detune an instrument plays at gets a sample of
its own, rendered with the offset on the chip:

    notes of inst 9:   detune 0 ×180   detune -20 ×12   detune -30 ×2
                         │                │                │
                         ▼                ▼                ▼
    samples:           inst 9           inst 23          inst 24        (free slots)
                       (own: 0)         (variant -20)    (variant -30)

The detune most of an instrument's notes play at is its own (the sample in its slot is
rendered with it; ties go to the smaller offset): a channel_instrument_map instrument made
for a detuned double (Title Screen FM5) needs no slot more.  Every other detune takes a free
slot, the most played first; one that finds none plays the instrument's own sample (warned).

A variant shares its instrument's map entry, level and sample_list volume and finetune: it
is the same sample, a few cents off.  The offset is in FNUM units, so its interval depends on
the note (fnum 606 … 1148 within the table's octave); the variant is rendered at the instrument's
synthesis pitch, and resampling carries that interval to every note.

A tie (smpsNoAttack + a duration) re-writes the frequency with the Detune in force, so a
scoop rises on its tie while the MOD note keeps its sample: the converter moves that note's
period with E1x / E2x on the tie's row (SmpsToModConverter._fine_slide).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..chips import FREQ_WORD_MAX, split_freq_word
from ..config import SAMPLE_FILE, SAMPLE_SLOT, SAMPLE_VOLUME
from ..smps import fm_table_index
from .driver_state import enabled_channels, walk_channel
from .instruments import fm_catalogue, free_slots

_NAME_CHARS = 22            # a MOD sample name


@dataclass(slots=True)
class DetuneVariant:
    """A slot holding an instrument's sample rendered at another detune."""
    inst: int                # its slot
    base: int                # the instrument it is a detuned copy of
    detune: int              # FNUM offset it is rendered with
    notes: int               # notes that play it


@dataclass
class DetunePlan:
    """Which sample each (instrument, detune) note plays.  Set on `config.detune_plan` by the
    converter; read by core.plan.driver_state.resolve_note and core.plan.instruments.fm_catalogue."""
    own: dict[int, int] = field(default_factory=dict)        # {instrument: its sample's detune}, nonzero only
    variants: dict[tuple[int, int], DetuneVariant] = field(default_factory=dict)   # (base, detune) ->
    unplaced: dict[tuple[int, int], int] = field(default_factory=dict)  # (base, detune) -> notes with no slot
    _bases: dict[int, int] = field(default_factory=dict)   # {variant slot: base}

    def instrument_for(self, inst: int, detune: int) -> int:
        """The slot a note of `inst` at `detune` plays."""
        v = self.variants.get((inst, detune))
        return v.inst if v is not None else inst

    def base_of(self, inst: int) -> int:
        """The instrument a variant is a copy of; any other instrument itself."""
        return self._bases.get(inst, inst)

    def share_base(self, per_inst: dict) -> dict:
        """`per_inst` with every variant given its base instrument's value (a level, a render
        level): a variant is its base's sample a few cents off."""
        out = dict(per_inst)
        for v in self.variants.values():
            if v.base in out:
                out[v.inst] = out[v.base]
        return out

    def add(self, v: DetuneVariant) -> None:
        self.variants[(v.base, v.detune)] = v
        self._bases[v.inst] = v.base


def detune_variants_wanted(synth) -> bool:
    """True where FM is synthesised and settings.yaml fm_synthesis.detune_variants allows
    variants (a SynthesisSettings, or None)."""
    return synth is not None and synth.enabled and synth.detune_variants


def plan_detune_variants(song, config) -> DetunePlan:
    """Count every FM note's (instrument, detune) and give each detune but the instrument's own
    a free slot.  Run on the song as parsed (the converter plans before its loop extension, so a
    tool that parses the song plans the same).  Only synthesised single-voice instruments (the FM catalogue) are planned: a
    sample loaded from disk cannot be re-rendered.  Appends each variant's sample_list entry
    (its base's, renamed) and sets `config.detune_plan`."""
    config.detune_plan = None                      # walked undetuned: every note names its base
    synthesised = set(fm_catalogue(song, config).instruments)

    counts: dict[int, dict[int, int]] = {}
    for chan_cfg, channel in enabled_channels(song, config, ("FM",)):
        for _event, _st, res in walk_channel(channel, config, chan_cfg):
            if res is None or res.instrument not in synthesised:
                continue
            per = counts.setdefault(res.instrument, {})
            per[res.detune] = per.get(res.detune, 0) + 1

    plan = DetunePlan()
    wanted: list[tuple[int, int, int]] = []        # (notes, base, detune)
    for inst, per in counts.items():
        own = max(per, key=lambda d: (per[d], -abs(d), d))
        if own:
            plan.own[inst] = own
        wanted += [(n, inst, d) for d, n in per.items() if d != own]

    # The most played first: a song short of slots loses its rarest detunes
    slots = free_slots(config, song)
    for notes, inst, d in sorted(wanted, key=lambda w: (-w[0], w[1], w[2])):
        if not slots:
            plan.unplaced[(inst, d)] = notes
            continue
        plan.add(DetuneVariant(slots.pop(0), inst, d, notes))

    entries = {e[SAMPLE_SLOT]: e for e in (config.sample_list or [])}
    if plan.variants and config.sample_list is None:
        config.sample_list = []
    for v in plan.variants.values():
        base = entries.get(v.base)
        name = f"{_stem(base[SAMPLE_FILE]) if base else f'fm_inst{v.base}'} dt{v.detune:+d}"[:_NAME_CHARS]
        config.sample_list.append([v.inst, name, *(base[SAMPLE_VOLUME:] if base else [])])

    config.detune_plan = plan
    return plan


def _stem(filename: str) -> str:
    return filename.rsplit(".", 1)[0]


def detune_cents(semitone: int, fnum_offset: int, fm_frequencies: tuple[int, ...]) -> float:
    """Cents an FNUM offset moves a note (SMPS semitone, C0 = 0) the driver's table plays
    (`fm_frequencies`: the song's): the offset is added to the whole block|fnum word, as
    FMUpdateFreq adds it."""
    i = max(0, min(len(fm_frequencies) - 1, fm_table_index(semitone)))
    word = fm_frequencies[i]
    moved = max(0, min(FREQ_WORD_MAX, word + fnum_offset))
    return 1200.0 * math.log2(_word_hz(moved) / _word_hz(word))


def _word_hz(word: int) -> float:
    """Relative frequency of a block|fnum word: fnum × 2^block."""
    fnum, block = split_freq_word(word)
    return max(1, fnum) * (1 << block)
