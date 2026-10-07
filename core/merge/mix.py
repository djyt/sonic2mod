"""The mixed composites' samples: each layer resampled by its period ratio, cut at its fill, summed,
looped where the primary loops (mix_pcm_composites, _Mixer)."""

from __future__ import annotations

import math

from ..audio import (
    DEFAULT_DITHER,
    DEFAULT_TAPS,
    FLAT_DB,
    INT8_PEAK,
    MIN_LOOP_SECS,
    RELEASE_FLOOR_DB,
    apply_loop,
    db_to_gain,
    find_sustain_loop,
    gain_to_db,
    high_shelf,
    limit_peaks,
    peak,
    resample,
    signed8,
    to_int8,
    unroll_values,
)
from ..config import DEFAULT_SHELF_HZ
from ..mod import MAX_MOD_SAMPLE_BYTES, PERIOD_TABLE, ModSample, clamp_mod_volume
from .model import Composite, MergePlan

# --- mixing the pcm composites -------------------------------------------------------------


_FADE_SECS = 0.002         # a cut with no release to speak of fades over this (a click otherwise)


_MIX_CROSS_SECS = 0.08     # a looped mix's crossfade: its layers beat, so the join lands on another


                           # phase of the beat, which 15 ms would step across and 80 ms blends
UPSAMPLE_TAPS = 12         # a layer resampled UP into a mix (a kick at 8 kHz under a hat at 28) gets a short


                           # kernel: a 32-tap sinc rings 2 ms before every transient, and the hat, at the
                           # mix's own rate, does not - so the drum's attack sat late behind the hat's


def _cut_layer(sig: list[float], keep: int, rate: float, release_db_s: float | None) -> list[float]:
    """A follower layer keyed off `keep` samples in: what follows decays at the voice's release
    rate (dB/s, from core.audio.loops) to the 8-bit floor (RELEASE_FLOOR_DB: past it the tail is
    quantisation noise), or is cut over 2 ms where the voice has no release to speak of (a PSG
    note ends the instant its attenuation is set to 15)."""
    if keep >= len(sig):
        return sig
    if release_db_s is None or not math.isfinite(release_db_s) or release_db_s <= 0:
        fade = max(1, int(rate * _FADE_SECS))
        tail = [v * (1 - i / fade) for i, v in enumerate(sig[keep:keep + fade])]
        return sig[:keep] + tail
    n = int(rate * RELEASE_FLOOR_DB / release_db_s)      # samples to the floor
    tail = [v * db_to_gain(-release_db_s * (i / rate)) for i, v in enumerate(sig[keep:keep + n])]
    return sig[:keep] + tail


def mix_pcm_composites(plan: MergePlan, mod, amiga_clock: float,
                       max_bytes: int = MAX_MOD_SAMPLE_BYTES,
                       hold_secs: dict[int, float] | None = None,
                       sources: dict[int, ModSample] | None = None,
                       release_db_s: dict[int, float | None] | None = None,
                       bank_out: dict[int, ModSample] | None = None,
                       raw: dict[int, tuple] | None = None,
                       raw_out: dict[int, list[float]] | None = None,
                       padding_secs: float = 0.0, loop_drift_db: float = FLAT_DB,
                       taps: int = DEFAULT_TAPS, shelf_hz: float = DEFAULT_SHELF_HZ,
                       dither: str = DEFAULT_DITHER, entry_dithers: dict[int, str] | None = None) -> list[dict]:
    """Build every mixed composite from the samples now in `mod`.

    A MOD sample triggered at note n plays at amiga_clock / PERIOD[n] whatever rate it was
    made at, so every layer is resampled by the period ratio of its note and the composite's
    trigger note (the fastest layer's, `Composite.note`; the primary's otherwise) and added at
    its sample_list volume times its level gain.
    The sum is peak-normalised and the composite's volume set so it plays at the sum's level;
    a sum past full scale keeps volume 64 and is reported (`headroom_db`).  Returns one dict
    per problem (a missing sample).

    A source is the sample installed in its slot, or the one `sources` ({instrument: ModSample})
    holds for a mix-only source whose slot a composite took (`MergePlan.mix_only`); a looped
    source (its own loop header) is handled as follows.  A looped follower is unrolled under the primary.  A looped primary mixed at its own
    rate keeps its loop, moved past the followers' tails: the unrolled data repeats the loop
    body, so any later repeat of it is the same seamless loop, and the composite is the
    followers' length plus one loop.  Mixed at another rate (resampled) the loop points would
    not land on samples, so the primary is unrolled for the composite's own longest note plus
    `padding_secs` instead (`Composite.longest`; `hold_secs`, {instrument: seconds}, the
    instrument-wide figure, only when the plan had no clock) and the mix plays straight
    through.  Green Hill's bridge lead holds 2.8 s notes under a chime a twelfth up, and its
    verse chords play 0.35 s ones under a chime; one figure per instrument served neither.  A follower the driver keyed
    off with smpsNoteFill (the key's fill) is cut there and decays at its instrument's release
    rate (`release_db_s`, {instrument: dB/s}; a bass pluck under a kick).  A banked composite's
    sample goes to `bank_out` ({provisional id: sample}) for core.merge.banks to pack, not into a slot.

    A composite with a `chip_base` (fm_on_chip) mixes that render - its primary and FM followers
    on the chip together, unlooped - in place of the primary's sample and those followers'; the
    render comes in `sources` and `raw` under its own id, and plays at the primary's volume times
    `chip_gain`, so its primary layer is as loud as the primary's sample would have been.

    A synthesised source is taken from `raw` ({instrument: (render values, rate)}, the
    generators' output before it was quantised to 8 bits, scaled as its sample was) rather than
    from the bytes in its slot, so a mix is quantised once, here — or, for a banked composite,
    once in core.merge.banks: its normalised sum goes to `raw_out` ({provisional id: values}) and the
    bank's volume scaling is applied before that quantisation.  A drum comes off disk as bytes,
    unless it was saturated (`saturate_db`): then its shaped values are in `raw` too.
    """
    mixer = _Mixer(mod, amiga_clock, hold_secs or {}, sources or {}, release_db_s or {}, raw or {}, padding_secs,
                   loop_drift_db, taps, shelf_hz)
    problems: list[dict] = []
    for comp in plan.composites.values():
        if comp.fm is not None:
            continue
        mixed = mixer.mix(comp, problems)
        if mixed is None:
            continue

        # Into its slot, or to core.merge.banks, which packs (and quantises) it
        sample, total, pk = _to_sample(comp, *mixed, max_bytes, composite_dither(comp, entry_dithers or {}, dither))
        if comp.banked and bank_out is not None:
            bank_out[comp.inst] = sample
            if raw_out is not None and pk:
                raw_out[comp.inst] = [v * 127.0 / pk for v in total[:len(sample.data)]]
        else:
            mod.samples[comp.inst - 1] = sample

    for inst in plan.blank_after_mix:            # a source no note plays once its composites exist
        mod.samples[inst - 1] = ModSample("")
    return problems


_CUT_NOTE_PAD_SECS = 0.05   # a PSG or drum primary's note is cut at its end: a row of the followers' tails


class _Mixer:
    """mix_pcm_composites' sources and settings; mix() sums one composite."""

    def __init__(self, mod, amiga_clock: float, hold_secs: dict[int, float], sources: dict[int, ModSample],
                 release_db_s: dict[int, float | None], raw: dict[int, tuple], padding_secs: float,
                 loop_drift_db: float = FLAT_DB, taps: int = DEFAULT_TAPS,
                 shelf_hz: float = DEFAULT_SHELF_HZ):
        self._mod, self._clock = mod, amiga_clock
        self._taps, self._shelf_hz = taps, shelf_hz
        self._hold_secs, self._sources = hold_secs, sources
        self._release, self._raw, self._padding = release_db_s, raw, padding_secs
        self._drift = loop_drift_db

    def _sample_of(self, inst: int) -> ModSample:
        return self._sources.get(inst) or self._mod.samples[inst - 1]

    def _rate(self, index: int) -> float:
        return self._clock / PERIOD_TABLE[index]

    def _tail_secs(self, inst: int) -> float:
        """How long a layer keyed off at the composite's end still sounds: its release to the
        floor, or the 2 ms fade of a voice with none."""
        r = self._release.get(inst)
        return RELEASE_FLOOR_DB / r if r is not None and math.isfinite(r) and r > 0 else 0.002

    def _values_of(self, inst: int, s: ModSample) -> list[float]:
        """The sample's values at the scale of its bytes: the unquantised render where the
        generator kept one (peak-normalised as the sample was, cut where the sample was), else
        its bytes."""
        r = self._raw.get(inst)
        if r is not None:
            mono, _rate = r
            pk = peak(mono)
            if pk:
                k = 127.0 / pk
                return [v * k for v in mono[:len(s.data)]]
        return list(signed8(s.data))

    def mix(self, comp: Composite, problems: list[dict]) -> tuple[list[float], tuple[int, int] | None, int] | None:
        """(sum, loop to keep, finetune) of one composite, at its trigger note's rate; None when
        its primary has no sample."""
        p_inst = comp.primary
        base = self._sample_of(p_inst)
        if not base.data:
            problems.append({'instrument': comp.inst, 'missing': p_inst})
            return None
        r_p = self._rate(comp.note if comp.note is not None else comp.base)

        # fm_on_chip: the primary's sample gives way to the render of all the FM layers (the
        # primary's volume and finetune still hold: the render is its sample's pitch and level)
        src, gain = p_inst, 1.0
        if comp.chip_base is not None and self._sample_of(comp.chip_base.inst).data:
            src, gain = comp.chip_base.inst, comp.chip_gain

        # How long a looped layer is unrolled: this composite's own longest note plus the release
        # padding.  The instrument-wide sustain is the fallback when the plan had no clock: a
        # chord mix used to be unrolled for its voice's 4 s song-wide need to play 0.35 s notes.
        # The release padding is for an FM primary, whose note ends in a release slide the sample
        # must still carry; a PSG or drum primary's note is cut at its end (or ends by itself)
        fm_primary = comp.group.primary.startswith("FM")
        pad = self._padding if fm_primary else min(self._padding, _CUT_NOTE_PAD_SECS)
        longest = comp.longest_played if comp.chip_base is not None else comp.longest
        need = longest + pad if longest else 0.0

        # The followers first: how long the mix has to run before a loop may start
        layers = self._follower_layers(comp, r_p, need, problems)
        total, keep_loop = self._primary_signal(comp, base, src, gain, r_p, need,
                                                max((len(sig) for sig in layers), default=0))

        for sig in layers:
            if len(sig) > len(total):
                total.extend([0.0] * (len(sig) - len(total)))
            for i, v in enumerate(sig):
                total[i] += v

        # The group's brightness shelf, on the whole sum (a drum off disk too)
        g = comp.group
        if g.treble_shelf_db:
            total = high_shelf(total, round(r_p), g.treble_shelf_hz or self._shelf_hz, g.treble_shelf_db)

        # The group's limiter: peaks past full scale at volume 64 (INT8_PEAK in the sum's units)
        # come down to it, so the sound keeps its level instead of all of it playing quieter
        if g.limit_db:
            total, comp.limited_db = limit_peaks(total, round(r_p), INT8_PEAK, g.limit_db)

        # Past what any note reaches, nothing is heard (the layers' release tails ran on there): a
        # note is heard to the earlier of its end plus the release slide (an FM primary's lasts
        # until the voice has fallen to the floor) and the column's next note-on
        # A kept loop no note reaches goes too (Green Hill lofi: 25 KB kept a lead's loop 0.8 s in
        # for one 0.2 s note)
        if comp.heard:
            slide = min(self._padding, self._tail_secs(p_inst)) if fm_primary else pad
            keep = math.ceil(max(min(end + slide, nxt) * speed for end, nxt, speed in comp.heard) * r_p)
            if keep_loop is not None and keep <= keep_loop[0]:
                keep_loop = None
            if keep_loop is None:
                total = _cut_layer(total, keep, r_p, None)

        # loop_mix: a long unlooped mix loops where its sum settles (lossy)
        if comp.group.loop_mix and keep_loop is None:
            loop = self._mix_loop(comp, total, r_p)
            if loop is not None:
                total = apply_loop(total, loop)
                keep_loop = (loop.start, loop.length)
        return total, keep_loop, base.finetune

    def _mix_loop(self, comp: Composite, total: list[float], r_p: float):
        """A sustain loop in the finished mix, found as a single voice's is (core.audio.loops): flat
        within the group's (or the song's) loop_drift_db of where its longest note ends, at least
        the group's loop_min_ms long, and ending before the mix does, else None (a mix that never
        settles, or no shorter for a loop).  The chord's layers beat, so a loop may need to span a
        beat: the finder tries up to MAX_LOOP_SECS.

            attack ─── settles ═══ loop ═══╗   the MOD repeats [start, end) until the note ends
                                 start ◄───╝
        """
        if comp.pitch_hz is None or not total:
            return None
        g = comp.group
        end = len(total) - max(1, int(r_p * _FADE_SECS)) - 1          # before the trim's fade
        ref = min(end, math.ceil(comp.longest * r_p)) if comp.longest else end
        return find_sustain_loop(total, round(r_p), r_p / comp.pitch_hz, end, ref_n=ref, max_end=end,
                                 flat_db=g.loop_drift_db if g.loop_drift_db is not None else self._drift,
                                 cross_secs=_MIX_CROSS_SECS, timbre=False,
                                 min_loop_secs=g.loop_min_ms / 1000.0 if g.loop_min_ms else MIN_LOOP_SECS,
                                 min_start_secs=(g.loop_start_ms or 0.0) / 1000.0)

    def _follower_layers(self, comp: Composite, r_p: float, need: float, problems: list[dict]) -> list[list[float]]:
        """Every follower's signal at its level, cut where it is keyed off, at the mix's rate."""
        layers: list[list[float]] = []
        for i, lay in enumerate(comp.key.layers):
            if i in comp.chip_layers and comp.chip_base is not None and self._sample_of(comp.chip_base.inst).data:
                continue                              # in the primary's chip render (fm_on_chip)
            f_inst, scale, fill_ms = lay.instrument, lay.scale, lay.fill_ms
            fs = self._sample_of(f_inst)
            if not fs.data:
                problems.append({'instrument': comp.inst, 'missing': f_inst})
                continue
            gain = fs.volume / 64.0 * scale
            r_f = self._rate(comp.base + lay.interval)
            f_data = self._values_of(f_inst, fs)

            # A looped follower is unrolled for the longer of the two instruments' longest notes
            # (a drum primary has no hold: until 2026-09-28 a looped bass under a kick was
            # unrolled to two bytes and vanished from every drum+bass mix), never shorter than the
            # sample, plus the release it gets when keyed off at the composite's end
            f_loop = _loop_of(fs)
            if f_loop is not None:
                hold = ((need + self._tail_secs(f_inst)) if need
                        else max(self._hold_secs.get(comp.primary, 0.0), self._hold_secs.get(f_inst, 0.0)))
                f_data = unroll_values(f_data, f_loop, max(len(f_data), int(hold * r_f) + 2))
            sig = [v * gain for v in f_data]

            # Keyed off by its note fill while the primary plays on
            if fill_ms is not None:
                sig = _cut_layer(sig, int(r_f * fill_ms / 1000.0), r_f, self._release.get(f_inst))

            # No layer outlasts the composite's own notes: keyed off there, with its release (a
            # hard cut left a click on every kick whose bass rang the whole note)
            if need and len(sig) > int(need * r_f) + 2:
                sig = _cut_layer(sig, int(need * r_f), r_f, self._release.get(f_inst))

            if round(r_f) != round(r_p):
                sig = resample(sig, round(r_f), round(r_p), taps=UPSAMPLE_TAPS if r_f < r_p else self._taps)
            layers.append(sig)
        return layers

    def _primary_signal(self, comp: Composite, base: ModSample, src: int, gain: float, r_p: float, need: float,
                        tail: int) -> tuple[list[float], tuple[int, int] | None]:
        """The primary's signal at its volume and the mix's rate, and the loop it keeps.  `src`
        is the render taken (the primary's own, or its chip_base's, at `gain` over its volume).

        A looped primary mixed at its own rate keeps its loop, moved past the followers' `tail`
        (the unrolled data repeats the loop body, so a later repeat is the same seamless loop).
        Its longest note ending before the loop starts: no loop, the notes plus the release
        (Green Hill's 0.35 s chords on a voice that settles 2.4 s in).  At another rate the loop
        points would not land on samples: unrolled for the notes and played straight through."""
        p_inst = comp.primary
        r_base = self._rate(comp.base)
        same_rate = round(r_base) == round(r_p)
        s = self._sample_of(src)
        b_data = self._values_of(src, s)
        keep_loop = None
        b_loop = _loop_of(s)
        if b_loop is not None:
            s0, ln = b_loop
            if need and int(need * r_base) < s0:
                b_data = b_data[:int((need + self._tail_secs(p_inst)) * r_base) + 2]
            elif same_rate and ln >= 4:
                k = max(0, -(-(tail - s0) // ln))          # repeats of the loop body before the tail ends
                b_data = unroll_values(b_data, b_loop, s0 + (k + 1) * ln)
                keep_loop = (s0 + k * ln, ln)
            else:
                hold = (need + self._tail_secs(p_inst)) if need else self._hold_secs.get(p_inst, 0.0)
                b_data = unroll_values(b_data, b_loop, max(len(b_data), int(hold * r_base) + 2))

        total = [v * base.volume / 64.0 * gain for v in b_data]
        if need and keep_loop is None and len(total) > int(need * r_base) + 2:
            total = _cut_layer(total, int(need * r_base), r_base, self._release.get(p_inst))
        if not same_rate:
            total = resample(total, round(r_base), round(r_p), taps=UPSAMPLE_TAPS if r_base < r_p else self._taps)
        return total, keep_loop


def _loop_of(s: ModSample) -> tuple[int, int] | None:
    """(start, length) in bytes of a sample's loop; None when it does not loop."""
    return (s.repeat * 2, s.repeat_length * 2) if s.repeat_length > 1 else None


def composite_dither(comp: Composite, entry_dithers: dict[int, str], default: str) -> str:
    """A mix's quantisation: its group's `dither:`, else its primary's entry's (as a chip
    composite's, FmInstrument.dither_mode), else settings.yaml's."""
    return comp.group.dither or entry_dithers.get(comp.key.primary) or default


def _to_sample(comp: Composite, total: list[float], keep_loop: tuple[int, int] | None, finetune: int,
               max_bytes: int, dither: str) -> tuple[ModSample, list[float], float]:
    """(sample, sum, peak): the sum peak-normalised to 8 bits at the volume that plays it at its
    level (64 and `comp.headroom_db` past full scale), cut to `max_bytes`, its loop kept."""
    pk = peak(total)
    if pk == 0:
        pcm = bytes(len(total))
        vol = 0
    else:
        pcm = to_int8(total, 127.0 / pk, dither)
        level = 64.0 * pk / 127.0
        vol = clamp_mod_volume(level)
        if level > 64:
            comp.headroom_db = gain_to_db(pk / 127.0)
    pcm = pcm[:max_bytes]
    if len(pcm) % 2:
        pcm += b"\x00"

    sample = ModSample(comp.entry[1] if comp.entry else f"merge{comp.inst}")
    sample.data = pcm
    sample.length = len(pcm) // 2
    sample.set_volume(vol)
    sample.set_finetune(finetune)
    if keep_loop is not None and keep_loop[0] + keep_loop[1] <= len(pcm):
        sample.repeat, sample.repeat_length = keep_loop[0] // 2, keep_loop[1] // 2
    if comp.entry is not None:
        comp.entry[2] = vol
    return sample, total, pk
