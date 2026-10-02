"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.
"""

import bisect
import copy
import dataclasses
import math

from .banks import pack_banks
from .config import (
    DEFAULT_SHELF_HZ,
    ChannelConfig,
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    rate3_synth_root_issues,
)
from .detune import DetunePlan, detune_cents, detune_variants_wanted, plan_detune_variants
from .driver_state import (
    DriverState,
    ResolvedNote,
    enabled_channels,
    resolve_synth_roots,
    walk_channel,
)
from .driver_state import source_map as source_map_for
from .driver_tables import PSG_ENVELOPES_BY_NAME, PSG_FREQUENCIES_EXTENDED, noise_envelope_frames, psg_tone2_divider
from .generators import SampleGenerators
from .instruments import fm_catalogue, psg_catalogue
from .levels import (
    DEFAULT_FM_PAN_LAW_DB,
    MOD_MAX_VOLUME,
    clamp_mod_volume,
    db_to_mod_volume,
    fm_level_db,
    fm_tl_to_mod,
    headroom_db,
    modal_level,
    psg_att_to_mod,
)
from .loops import FLAT_DB, SustainLoop
from .merge import NO_SLOT, Composite, MergePlan, build_merge_plan, mix_pcm_composites
from .mod import ModFile, ModSample, apply_pattern_breaks, row_to_bcd
from .pcm import DEFAULT_DITHER, INT8_PEAK, MAX_MOD_SAMPLE_BYTES, max_sustain_secs, peak, saturate, signed8, to_int8
from .resample import DEFAULT_TAPS
from .smps_parser import SmpsChannel, SmpsSong
from .tables import (
    MOD_NOTE_MAP as _MOD_NOTE_MAP,
)
from .tables import (
    PERIOD_TABLE,
    ModNote,
)
from .tables import (
    semitone_to_note_name as _semitone_to_name,
)

# Sonic 1 base FNUM for note C, from the MakeFMFrequency table (644 for C ... 1216 for B).
# The 11-bit FNUM is the same across all octave blocks — block just shifts the register.
# smpsModSet adds its swing to the note's own FNUM: see SmpsToModConverter._vibrato_depth.
_S1_FNUM_BASE = 644

# A 4xy's swing at its sine table's peak (255), in Amiga periods, per player (settings.yaml `player`):
#   PT2  (255 * y) >> 7 whole periods             2y - 1      y=1: 1   y=2: 3     y=3: 5
#   FT2  (255 * y) >> 5 quarter periods, i.e. /4  2y - 1/4    y=1: 1.75 y=2: 3.75 y=3: 5.75
_VIBRATO_PEAK = {
    "pt2": lambda y: (255 * y) >> 7,
    "ft2": lambda y: ((255 * y) >> 5) / 4,
}



def _shift_for_breaks(flat_row: int, breaks: list[tuple[int, int]] | None) -> int:
    """Move a pre-break flat row index to where apply_pattern_breaks put it.

    Each break at (pattern P, row R) pushes everything from flat row P*64+R+1 onward
    to the start of pattern P+1, i.e. forward by the 63-R rows it blanked out.
    """
    for P, break_row in sorted(breaks or []):
        if flat_row >= P * 64 + break_row + 1:
            flat_row += 63 - break_row
    return flat_row


_HEADROOM_REPORT_DB = 0.05     # a composite clamped by less is not reported
_FINE_SLIDE_MAX = 0xF          # E1x / E2x move the period by at most 15 units


def _cut_ring(rings: list, tick: int) -> None:
    """The ring still sounding ends at `tick`: a note-on retriggers the column (in the merged
    build a pooled note can take it before the ring's own duration is up: cut_after)."""
    if rings and rings[-1] is not None:
        rings[-1][1] = min(rings[-1][1], tick - rings[-1][0])


class _ColumnRouter:
    """Where one channel's notes go in the merged build (core.merge.MergePlan): its own column,
    or the one a merge_patterns group routes it to in the note's pattern; which of its notes
    play on another channel; what a drum hit plays there.  Without a plan: its own column,
    every note, as written.

        pattern:   1-4        5-c        d-10
        FM5     →  col 1      (folded)   col 4    ← mod_channel / fill routes
    """

    def __init__(self, conv: "SmpsToModConverter", source: str, home: int):
        self._conv, self._plan = conv, conv._merge
        self._source, self._home = source, home
        self._last: int | None = None      # the column the previous note-on went to
        self._away = self._plan.away_patterns(source) if self._plan is not None else frozenset()

    def _pattern(self, tick: int) -> int:
        return self._conv._pattern_of_tick(tick)

    def column_for(self, tick: int) -> int:
        """The column a note-on at `tick` takes (the reference build's pattern, as the groups
        count them)."""
        if self._plan is None:
            return self._home
        r = self._plan.route_at(self._source, self._pattern(tick))
        return self._home if r is None else r

    def current(self, tick: int) -> int:
        """The column this channel's ring is on: the last note-on's."""
        return self._last if self._last is not None else self.column_for(tick)

    def take(self, pattern: int, row: int, tick: int, sounding: bool) -> int:
        """The column a note-on at `tick` goes to; a note still ringing on another column
        (`sounding`, the previous block's) is cut there, as the re-key ended it."""
        chan = self.column_for(tick)
        last = self._last
        if last is not None and last != chan and sounding and not self._conv.mod.note_at(pattern, row, last):
            self._conv._set_cursor(pattern, last, row)
            self._conv.mod.set_effect(0xC, 0)
        self._last = chan
        return chan

    def plays_here(self, ev) -> bool:
        """A note-on this channel's output sounds: not one folded onto another channel (or
        dropped) in its pattern."""
        return (self._plan is None or getattr(ev, "merged", None) is not None
                or not self._plan.is_folded(self._source, ev.tick_position))

    def away(self, ev) -> bool:
        """An own event in a pattern this channel plays nothing of its own in (a follower's, or
        dropped there)."""
        return (bool(self._away) and getattr(ev, "merged", None) is None
                and self._pattern(ev.tick_position) in self._away)

    def borrowed(self, column: int, tick: int) -> bool:
        """Another channel's notes take `column` at `tick` (mod_channel).  A group routing this
        channel's own notes there is not a borrow: its rests release on their own column."""
        if self._plan is None:
            return False
        owner = self._plan.routed_into(column, self._pattern(tick))
        return owner is not None and owner != self._source

    def note(self, tick: int, index: int) -> int:
        """The MOD note a primary note at `tick` is triggered at: a transposed mix's own."""
        return index if self._plan is None else self._plan.note_at(self._source, tick, index)

    def drum(self, tick: int, inst: int, note: ModNote) -> tuple[int, ModNote, tuple[int, int] | None]:
        """(instrument, note, bank region) a drum hit plays: a composite with its hi-hat folded
        in, and where its sound starts in a sample bank."""
        if self._plan is None:
            return inst, note, None
        return (self._plan.instrument_at(self._source, tick, inst),
                ModNote(self._plan.note_at(self._source, tick, note.value)),
                self._plan.region_at(self._source, tick))


# merge_bank_slots: auto builds at most this often (each build renders every mix again)
_MAX_BANK_BUILDS = 4


class SmpsToModConverter:
    def __init__(self, song: SmpsSong, config: ConversionConfig,
                 synth: SynthesisSettings | None = None,
                 psg_synth: PsgSynthesisSettings | None = None,
                 generators: SampleGenerators | None = None):
        # The chip packages' renderers, handed down from the layer above (core/generators.py);
        # needed only where synthesis is enabled.  Kept across convert()'s rebuilds.
        self._generators = generators or SampleGenerators()
        self._start(song, config, synth, psg_synth)

    def _start(self, song: SmpsSong, config: ConversionConfig,
               synth: SynthesisSettings | None, psg_synth: PsgSynthesisSettings | None) -> None:
        """A fresh conversion's state (convert() starts over with it)."""
        self.song = song
        self.config = config
        self.synth = synth
        self._player = synth.player if synth else "ft2"
        self.psg_synth = psg_synth
        self.mod = ModFile(channels=config.mod_channel_count)
        # Structured warnings and informational messages collected during conversion.
        # Public: convert.py renders both after convert() returns.
        self.warnings: list[dict] = []
        self.infos: list[dict] = []
        self._seen_warnings: set = set()
        self._leading_rest_channels: dict[int, str] = {}   # MOD channel -> source, see _place_leading_rests
        self._vib_rate_limited: set = set()
        # Sustain loops (core.loops, settings.yaml `sustain_loops`): the loop each synthesised
        # sample was cut to, and how fast each FM instrument's level falls after key-off.
        self._loops: dict[int, SustainLoop] = {}
        self._release: dict[int, float | None] = {}
        self._release_slides = False       # end FM notes with a volume slide instead of C00
        self._pending_sustain_short: dict[tuple[str, int], dict] = {}
        self._merge: MergePlan | None = None   # the merged build's plan; set by convert() (_build_merge_plan)
        self._detune: DetunePlan | None = None   # the detune variants; set by convert() (_plan_detune)
        self._sample_detunes: dict[int, float] | None = None   # {FM instrument: cents its sample is detuned}
        self._tie_retunes = {'placed': 0, 'skipped': 0}   # E1x / E2x on ties that change the detune
        self._mix_sources: dict[int, ModSample] = {}   # mix-only sources whose slot a composite holds
        self._raw_renders: dict[int, tuple] = {}       # {instrument: (render values, rate)} before 8-bit
        self._sample_rates: dict[int, int] = {}        # {instrument: Hz its synthesised sample was rendered at}
                                                       #   quantisation: what the composite mixer mixes from
        self._bank_delays_dropped = 0      # banked drum notes whose EDx gave way to the 9xx offset
        self._bank_cuts = 0                # banked notes cut before the next sound in their slot
        self._bank_cxx_moved = 0           # banked melodic notes whose attack-row Cxx went to the next row
        self._gained: dict[str, set[int]] = {}   # {"FM"/"PSG": instruments unison chords play louder}
        self._rings_out: dict[str, set[int]] = {}   # {"FM"/"PSG": instruments a channel's last note rings out on}
        self._slide_ends: dict[str, set[int]] = {}  # {"FM"/"PSG": instruments a note of ends in a release slide}
        self._idle_bank_slots: list[int] = []    # merge_bank_slots reserve the banks left empty while
                                                 #   composites went without a slot (convert re-runs)
        self._bank_retry: dict | None = None     # set on that second pass: what it was run for

    def _add_warning(self, w: dict):
        """Append a warning, deduplicating by (type, channel, extra_ctx, key)."""
        key = (
            w['type'],
            w.get('channel'),
            w.get('extra_ctx'),
            w.get('src_name') or w.get('note_name') or w.get('source'),
        )
        if key not in self._seen_warnings:
            self._seen_warnings.add(key)
            self.warnings.append(w)

    @property
    def _effective_tpr(self) -> float:
        """Ticks per row accounting for the global tempo divider.

        Parser stores note durations as raw_duration * chan_tempo_div (initialized
        to header.tempo_divider).  To convert stored ticks → rows we divide by
        yaml_tpr * global_divider, keeping BPM and YAML config unchanged.
        """
        return self.config.ticks_per_row * self.song.header.tempo_divider

    @property
    def _fm_volume_mode(self) -> str:
        """"baked" | "absolute" | "off" — see SynthesisSettings.fm_volume_mode."""
        return self.synth.fm_volume_mode if self.synth else "baked"

    @property
    def _psg_volume_mode(self) -> str:
        """"baked" | "absolute" — see PsgSynthesisSettings.psg_volume_scaling."""
        return self.psg_synth.psg_volume_scaling if self.psg_synth else "baked"

    def _vibrato_speed(self, mod_speed: int, steps: int, channel: str, tick=0) -> int:
        """ProTracker 4xy speed nibble for an smpsModSet (speed, steps) pair; 0 = cannot be played.

        Driver (DoModulation, once per V-int frame): every `speed` frames the delta is added; when
        the step counter runs out it is reloaded from the ORIGINAL steps byte, the delta is negated
        and one more step is spent.  Only the first half-swing uses the halved count, so the steady
        cycle is 2 * speed * (steps + 1) frames.  Not multiplied by the tempo divider.

        ProTracker: the vibrato position advances by x on each of a row's (speed - 1) processing
        ticks and wraps at 64.  One row is _effective_tpr driver ticks = _effective_tpr /
        _tpf_at(tick) frames, so matching the two cycle lengths gives

            x = 64 * _effective_tpr / ((target_speed - 1) * cycle_frames * _tpf_at(tick))

        Region-independent: both clocks scale with the frame rate.
        """
        rows_ticks = self.config.target_speed - 1
        if rows_ticks < 1:
            return 0
        cycle_frames = 2 * (mod_speed or 256) * (steps + 1)
        tpf = self._tpf_at(tick)
        exact = 64 * self._effective_tpr / (rows_ticks * cycle_frames * tpf)
        x = max(1, min(0xF, round(exact)))
        if exact > 15.5:
            # 4Fy is the fastest there is; say so once per channel and setting.
            key = (channel, mod_speed, steps)
            if key not in self._vib_rate_limited:
                self._vib_rate_limited.add(key)
                played = 64 * self._effective_tpr / (rows_ticks * 15 * tpf)
                self.infos.append({'type': 'vibrato_rate_limit', 'channel': channel,
                                    'wanted_cycle_frames': cycle_frames, 'played_cycle_frames': played})
        return x

    @staticmethod
    def _vibrato_depth(delta: int, steps: int, period: int, chip_index: int, is_psg: bool,
                       player: str = "ft2") -> int:
        """4xy depth nibble for one note in `player`'s replayer; 0 = too shallow to play.

        Driver: the accumulator swings delta * steps / 2 either side of its centre (first
        half-swing steps/2 steps, every later one the full `steps`), and is added to the note's
        own frequency word: the YM2612 FNUM of its pitch class (644 for C ... 1216 for B, the
        block is untouched) or the SN76489 divider of its PSGFrequencies entry.  An Amiga period
        and a PSG divider are both 1/f and a small FNUM change is proportional to f, so in every
        case the swing in periods is

            swing = period * (delta * steps / 2) / frequency_word

        The depth is the y whose peak in that player (_VIBRATO_PEAK) is nearest the swing: PT2
        truncates to whole periods, a round(swing / 2) depth there is a period short on every note
        (Green Hill's y=1 notes half as deep).  Below 0.7 periods the smallest depth would
        overshoot the hardware by 3x or more: no vibrato.
        """
        word = (PSG_FREQUENCIES_EXTENDED[chip_index & 0x7F] if is_psg
                else _S1_FNUM_BASE * 2 ** ((chip_index % 12) / 12))
        if word <= 0:
            return 0
        if delta >= 0x80:
            delta -= 0x100
        swing = period * (abs(delta) * steps / 2) / word
        if swing < 0.7:
            return 0

        peak = _VIBRATO_PEAK[player]
        return min(range(1, 0x10), key=lambda y: abs(peak(y) - swing))

    # --- global duration divider (smpsSetTempoDiv, $EB) -------------------------------------
    def _apply_global_tempo_div(self) -> list[tuple[int, int]]:
        """Re-time every channel for smpsSetTempoDiv (cfSetTempoDividerAll), which writes a new
        TempoDivider into EVERY track.  Returns the [(tick, divider)] changes found (Credits: $02
        then $01 in the DAC track, a half-tempo passage).

        The parser scaled each channel's durations by its OWN divider only (the header value and
        smpsChanTempoDiv).  The driver multiplies a duration by the track's divider when the note
        is READ, so a note begun before the change keeps its length and the first note read after
        it takes the new one; the last write wins, whether it was the track's own smpsChanTempoDiv
        or the global flag.  The carrying channel is re-timed first (the change's real tick
        depends on any earlier change), then the others.  Labels (loop targets) are not re-timed.
        """
        found = [ch for ch in self.song.channels
                 if any(ev.is_effect and ev.effect.effect_type == 'smpsSetTempoDiv' for ev in ch.events)]
        if not found:
            return []
        header_div = self.song.header.tempo_divider
        changes: list[tuple[int, int]] = []

        def retime(ch, changes):
            own_parse = header_div          # divider the parser used for the next note
            own_div, own_tick = header_div, -1
            act = 0
            new_changes = []
            for ev in ch.events:
                if ev.is_note:
                    d_raw = ev.note.duration / own_parse
                    g = [c for c in changes if c[0] <= act and c[0] > own_tick]
                    div = g[-1][1] if g else own_div
                    ev.tick_position = act
                    ev.note.duration = round(d_raw * div)
                    act += ev.note.duration
                else:
                    ev.tick_position = act
                    kind = ev.effect.effect_type
                    if kind == 'smpsChanTempoDiv':
                        own_parse = own_div = ev.effect.params[0]
                        own_tick = act
                    elif kind == 'smpsSetTempoDiv':
                        # Writes this track's divider too (cfSetTempoDividerAll covers every track).
                        own_div, own_tick = ev.effect.params[0], act
                        new_changes.append((act, ev.effect.params[0]))
            return new_changes

        for ch in found:
            changes = sorted(set(changes) | set(retime(ch, changes)))
        for ch in self.song.channels:
            if ch not in found:
                retime(ch, changes)
        return changes

    # --- mid-song tempo changes (smpsSetTempoMod, $EA) --------------------------------------
    def _collect_tempo_segments(self) -> list[tuple[int, int]]:
        """[(start tick, tempo modifier)] in tick order, the header's value first.

        cfSetTempo writes v_main_tempo for every track and restarts the TempoWait counter, so
        from that tick on ticks run at fps*(m-1)/m with the hold pattern starting afresh.
        Only the modifier changes here; the divider (smpsSetTempoDiv, $EB) is not applied.
        """
        segs = [(0, self.song.header.tempo_modifier)]
        for ch in self.song.channels:
            segs.extend((ev.tick_position, ev.effect.params[0]) for ev in ch.events
                        if ev.is_effect and ev.effect.effect_type == 'smpsSetTempoMod')
        segs.sort()
        out: list[tuple[int, int]] = []
        for t, m in segs:
            if out and out[-1][0] == t:
                out[-1] = (t, m)
            elif not out or out[-1][1] != m:
                out.append((t, m))
        return out

    def _segment_at(self, tick) -> tuple[int, int]:
        """(start tick, tempo modifier) of the tempo segment `tick` falls in."""
        segs = getattr(self, '_tempo_segments', None) or [(0, self.song.header.tempo_modifier)]
        i = bisect.bisect_right([s[0] for s in segs], tick) - 1
        return segs[max(i, 0)]

    def _tpf(self, modifier: int) -> float:
        """Duration ticks that elapse per V-int frame for a tempo modifier: (m - 1) / m.

        TempoWait fires once every `modifier` frames and only does `addq.b #1` on each
        track's DurationTimeout, cancelling that frame's decrement.  NoteTimeoutUpdate
        (smpsNoteFill) and DoModulation (smpsModSet wait/speed) still run on those frames,
        so they count FRAMES while note durations and tick positions count TICKS.  Multiply a
        frame count by this to place it on the converter's tick timeline.

        Region-independent (both clocks scale with fps).  SFX have no tempo modifier.
        Mid-song smpsSetTempoMod is tracked in _tempo_segments, so use _tpf_at(tick)
        wherever the tick is known and this only for an explicitly chosen modifier.
        """
        if self.song.header.is_sfx or modifier <= 1:
            return 1.0
        return (modifier - 1) / modifier

    def _tpf_at(self, tick) -> float:
        return self._tpf(self._segment_at(tick)[1])

    def _bpm_for(self, modifier: int) -> int:
        """MOD BPM for a tempo modifier: the song's BPM scaled by the change in tick rate."""
        base = self._tpf(self.song.header.tempo_modifier)
        return max(32, min(255, round(self.config.target_bpm * self._tpf(modifier) / base)))

    def _write_tempo_changes(self) -> None:
        """Fxx (set BPM) on the row of every smpsSetTempoMod, in a cell whose effect slot is free.

        Spare MOD channels are tried first, then any channel's cell without an effect, then a
        cell holding only a 4xy continuation (vibrato is the least of the three).  Drowning
        speeds up in four steps this way; the header tempo is still the song's own BPM.
        """
        used = {c.mod_channel for c in self.config.channels if c.enabled}
        order = [c for c in range(self.mod.CHANNELS) if c not in used] + sorted(used)
        for start, modifier in self._tempo_segments[1:]:
            bpm = self._bpm_for(modifier)
            exact = self.config.target_bpm * self._tpf(modifier) / self._tpf(self.song.header.tempo_modifier)
            pattern, row = self._tick_to_pattern_row(start)
            if pattern >= self.config.max_patterns:
                break
            self.mod.ensure_pattern(pattern)
            slot = None
            for want_free in (True, False):
                for ch in order:
                    eff, par = self.mod.effect_at(pattern, row, ch)
                    free = eff == 0 and par == 0
                    vib_only = eff == 0x4 and not self.mod.note_at(pattern, row, ch)
                    if free if want_free else vib_only:
                        slot = ch
                        break
                if slot is not None:
                    break
            info = {'type': 'tempo_change', 'tick': start, 'pattern': pattern, 'row': row,
                    'modifier': modifier, 'bpm': bpm, 'exact_bpm': exact}
            if slot is None:
                self._add_warning({'type': 'tempo_no_slot', 'channel': 'all', **info})
                continue
            self._set_cursor(pattern, slot, row)
            self.mod.set_effect(0xF, bpm)
            self.infos.append(info)
            if not 32 <= exact <= 255:
                self._add_warning({'type': 'tempo_bpm_range', 'channel': 'all', **info})

    def _tick_span_secs(self, start: float, end: float) -> float:
        """Seconds the MOD takes to play from tick `start` to tick `end`.

        A tick lasts target_speed x 2.5 / (BPM x ticks per row) seconds at the BPM in force,
        and smpsSetTempoMod changes that BPM mid-song (_bpm_for, the rounded value the Fxx
        writes), so the span is summed over the tempo segments it crosses.
        """
        segs = getattr(self, '_tempo_segments', None) or [(0, self.song.header.tempo_modifier)]
        total = 0.0
        for i, (seg_start, modifier) in enumerate(segs):
            seg_end = segs[i + 1][0] if i + 1 < len(segs) else float('inf')
            lo, hi = max(start, seg_start), min(end, seg_end)
            if hi > lo:
                total += ((hi - lo) * self.config.target_speed * 2.5
                          / (self._bpm_for(modifier) * self._effective_tpr))
        return total

    def _set_cursor(self, pattern: int, channel: int, row: int) -> None:
        """Position the MOD file cursor at (pattern, channel, row)."""
        self.mod.set_active_pattern(pattern)
        self.mod.set_channel(channel)
        self.mod.set_row(row)

    def _install_synthesized_samples(self, samples_dict: dict, sample_list, prefix: str,
                                     max_bytes: int = MAX_MOD_SAMPLE_BYTES) -> None:
        """Install synthesized PCM samples into mod.samples and apply sample_list overrides.
        `max_bytes` is the settings' sample limit (max_sample_kb); a longer sample is cut."""
        for inst_num, (pcm_orig, rate) in samples_dict.items():
            self._sample_rates[inst_num] = rate
            self.mod.samples[inst_num - 1] = self._make_sample(inst_num, pcm_orig, prefix,
                                                               self._loops.get(inst_num), max_bytes)

    def _make_sample(self, inst_num: int, pcm_orig: bytes, prefix: str, loop: SustainLoop | None,
                     max_bytes: int = MAX_MOD_SAMPLE_BYTES, original: bool = False) -> ModSample:
        """A ModSample for a synthesised PCM: cut to the limit, its loop header, and the
        sample_list volume and finetune for the instrument - the last entry naming the slot (a
        composite's, appended by the plan) unless `original` asks for the first (the source the
        slot was named for, kept aside for the mixer)."""
        sample_list = self.config.sample_list or []
        entries = [e for e in sample_list if e[0] == inst_num]
        entry = (entries[0] if original else entries[-1]) if entries else None
        pcm_data = pcm_orig[:max_bytes]
        if len(pcm_orig) > max_bytes:
            # The generators cap the sustain to the limit; this is a last resort.
            self._add_warning({'type': 'sample_truncated', 'channel': prefix,
                               'extra_ctx': f'instrument {inst_num}', 'instrument': inst_num,
                               'bytes': len(pcm_orig), 'max_bytes': max_bytes})
        if len(pcm_data) % 2:                   # a MOD sample is whole words: evened with a zero
            pcm_data += b"\0"
        sample = ModSample(entry[1] if entry else f"{prefix}_inst{inst_num}")
        sample.data = pcm_data
        sample.length = len(pcm_data) // 2
        sample.set_volume(entry[2] if entry and len(entry) > 2 else 64)
        if entry and len(entry) > 3 and entry[3] != 0:
            sample.set_finetune(entry[3])
        if loop is not None and loop.end <= len(pcm_data) and loop.length >= 4:
            sample.repeat = loop.start // 2
            sample.repeat_length = loop.length // 2
        return sample

    def sample_sources(self) -> dict[int, dict]:
        """{instrument: what its slot holds} for the report (core/report.py), once converted:
        `kind` (FM, PSG, noise, DAC, chip, mix, bank), `source` (the voice and range, the envelope,
        the DAC sample, the group a composite folds), `rate` (Hz it was rendered at, where it was
        synthesised) and `release` (dB/s its notes' release slides fall at).  The loop is the
        MOD header's."""
        out: dict[int, dict] = {}
        for d in self.config.dac_samples:
            out[d.mod_instrument] = {'kind': 'DAC', 'source': d.name}
        if self.synth is not None and self.synth.enabled:
            for inst in fm_catalogue(self.song, self.config).instruments.values():
                e = inst.entry
                src = f"${inst.layers[0].voice_idx:02X} {_semitone_to_name(e.low)}–{_semitone_to_name(e.high)}"
                if inst.source_label:
                    src += f" {inst.source_label}"
                if inst.layers[0].fnum_offset:
                    src += f" detune {inst.layers[0].fnum_offset:+d}"
                out[inst.inst] = {'kind': 'FM', 'source': src}
        if self.psg_synth is not None and self.psg_synth.enabled:
            for inst in psg_catalogue(self.config, {i: d['envelope'] for i, d in
                                                     self._derive_noise_envelopes().items()}).values():
                e = inst.entry
                env = e.envelope if isinstance(e.envelope, str) else ("inline" if e.envelope else "")
                if e.type == "tone":
                    out[inst.inst] = {'kind': 'PSG', 'source': inst.source or env}
                    continue
                white = "white" if e.type == "white_noise" else "periodic"
                out[inst.inst] = {'kind': 'noise', 'source': f"{white} {inst.source} {env}".strip()}
        if self._merge is not None:
            for c in self._merge.composites.values():
                if not c.banked:
                    out[c.inst] = {'kind': 'chip' if c.fm is not None else 'mix',
                                   'source': c.group.label + c.group.where}
            for b in self._merge.banks:
                groups = sorted({m.group.label for m in b.members})
                out[b.slot] = {'kind': 'bank', 'source': f"{len(b.members)} sounds · {', '.join(groups)}"}
        for inst, d in out.items():
            if inst in self._sample_rates and d['kind'] in ('FM', 'PSG', 'noise', 'chip'):
                d['rate'] = self._sample_rates[inst]     # a mix or a bank took its slot's number over
            if inst in self._release and self._release[inst] != float('inf'):
                d['release'] = self._release[inst]
        return out

    @property
    def _dither(self) -> str:
        """settings.yaml samples.dither: the quantisation of samples no entry or group overrides."""
        return self.synth.dither if self.synth else DEFAULT_DITHER

    def _entry_dithers(self) -> dict[int, str]:
        """{instrument: dither} for every synthesised instrument whose entry says `dither:` (the
        catalogues: what the generators render from); a mix falls back to its primary's."""
        out = {i.inst: i.dither_mode for i in fm_catalogue(self.song, self.config).instruments.values()
               if i.dither_mode}
        out.update({i.inst: i.entry.dither for i in psg_catalogue(self.config).values() if i.entry.dither})
        return out

    def _saturate_dac_samples(self) -> None:
        """Each `dac_samples` drum with a saturate_db (merge_saturate_db in the merged build)
        soft-clipped (core.pcm.saturate) and requantised to its full 8 bits: the same peak and
        volume, a louder body.  Before the mixes, which are built from it."""
        for d in self.config.dac_samples:
            db = d.saturation_db(self.config.merge_active)
            sample = self.mod.samples[d.mod_instrument - 1]
            if not db or sample is None or not sample.data:
                continue
            shaped = saturate(signed8(sample.data), db)
            sample.data = to_int8(shaped, INT8_PEAK / peak(shaped), self._dither)
            self.infos.append({'type': 'dac_saturated', 'instrument': d.mod_instrument, 'name': d.name, 'db': db})

    def _synthesis_roots(self, kind: str) -> dict[int, tuple[int, int]]:
        """{MOD instrument: (MOD note index its sample is synthesised for, synth_shift)}.

        The sample's own rate is `root`'s playback rate times 2^(synth_shift / 12) (the
        generators render synth_shift semitones above the pitch `root` sounds).  Read from the
        instrument catalogue (core.instruments), which is what the generators render from; an
        instrument absent here is not synthesised (loaded from disk).
        """
        if kind == "FM":
            return {i.inst: (i.rate_root_idx, i.synth_shift)
                    for i in fm_catalogue(self.song, self.config).instruments.values()}
        return {i.inst: (i.root_idx, i.entry.synth_shift)
                for i in psg_catalogue(self.config).values()}

    def _slides_after(self, chan_cfg, start: int, rest: int) -> bool:
        """Whether the rest at `rest` ending a note that started at `start` is written as a
        release slide, as _convert_channel writes it: in the merged build, not on a column a
        merge group routes notes onto there (a C00 then; a slide would sit on their notes)."""
        plan = self._merge
        if plan is None:
            return True
        col = plan.route_at(chan_cfg.source, self._pattern_of_tick(start))
        col = chan_cfg.mod_channel if col is None else col
        return plan.routed_into(col, self._pattern_of_tick(rest)) is None

    def _sustain_needs(self, kind: str) -> dict[int, tuple[float, tuple[int, int] | None]]:
        """{MOD instrument: (seconds of sample it must hold, synthesis root index or None)}
        over the enabled channels of `kind` ("FM" / "PSG").

        The seconds are the longest ring of any of the instrument's notes, measured at the
        sample's own synthesis rate.  A ring is a note plus the smpsNoAttack continuations
        after it: no C00 is written for those, so the sample keeps advancing; a plain rest
        (C00) or the next note restarts it.  One row is added, the most the row grid moves
        a note's start (EDx) or its end.  Its wall-clock length follows the MOD's tempo
        segments (_tick_span_secs).  Played above the synthesis root (_synthesis_roots) the
        sample runs faster by root period / note period and needs proportionally more of it,
        and a sample rendered synth_shift semitones above the root's pitch runs 2^(shift/12)
        slower at every note; a positive sample_list finetune adds 2^(finetune / 96).

        The MOD note and instrument are the ones _convert_channel will trigger: the same
        walk_channel / resolve_note.
        """
        finetunes = {e[0]: e[3] for e in (self.config.sample_list or []) if len(e) > 3}
        roots = self._synthesis_roots(kind)
        # A note that plays a mixed composite (core.merge) plays its source samples inside it:
        # the primary's at the note, each follower's at its interval.  They need the ring as much
        # as if the note were their own - the loop search and the sustain are theirs (Green Hill's
        # bridge lead lost its 2.8 s notes to the lead+chime mixes, and its sample was cut to a
        # loop 0.09 s in, at the attack's level).
        comps = ({c.inst: c for c in self._merge.composites.values() if c.fm is None}
                 if self._merge is not None else {})
        needs: dict[int, tuple[float, tuple[int, int] | None]] = {}
        for chan_cfg, channel in enabled_channels(self.song, self.config, (kind,)):
            if self._merge is not None and not chan_cfg.enabled:
                continue            # a follower: its notes play as composites (credited above) or
                                    # spliced onto a live channel (counted there), or not at all
            rings: list = []        # [start tick, ring ticks, instrument, out idx, ends in a slide] or None
            # A smpsNoAttack note is a 3FF (the sample rings on) unless `legato: retrigger`
            # writes it as a note-on: Drowning's FM3 trill, 240 legato notes, measured one 10 s
            # ring for notes of a second
            portamento = (self.synth.legato if self.synth else "strict") != "retrigger"
            for event, _st, res in walk_channel(channel, self.config, chan_cfg):
                if not event.is_note:
                    continue
                if (self._merge is not None and getattr(event, "merged", None) is None
                        and self._merge.is_folded(chan_cfg.source, event.tick_position)):
                    if not event.note.is_rest:
                        _cut_ring(rings, event.tick_position)   # the converter ends what rings here at it
                    rings.append(None)          # folded away here: nothing of its own sounds
                    continue
                note = event.note
                if note.is_rest:
                    if note.is_no_attack and rings and rings[-1] is not None:
                        rings[-1][1] += note.duration       # continuation: no C00, keeps advancing
                    else:
                        if rings and rings[-1] is not None:
                            rings[-1][4] = self._slides_after(chan_cfg, rings[-1][0], event.tick_position)
                        rings.append(None)                  # C00 (or a slide) ends the ring
                    continue
                if res is None:
                    continue
                if note.is_no_attack and portamento and rings and rings[-1] is not None:
                    rings[-1][1] += note.duration           # legato: a portamento, the sample rings on
                    continue
                _cut_ring(rings, event.tick_position)
                rings.append([event.tick_position, note.duration, res.instrument, res.index, False])

            # A channel's last note, with no rest or note after it, rings out into its sample's
            # release (a jingle's final chord): that instrument keeps its release padding
            if rings and rings[-1] is not None:
                comp = comps.get(rings[-1][2])
                self._rings_out.setdefault(kind, set()).update(
                    [rings[-1][2]] + ([i for i, _ in comp.mix_notes(0)] if comp is not None else []))
            for ring in rings:
                if ring is None:
                    continue
                start, ticks, inst, out_idx, slides = ring
                if slides:
                    self._slide_ends.setdefault(kind, set()).add(inst)
                wall = self._tick_span_secs(start, start + ticks + self._effective_tpr)
                comp = comps.get(inst)
                plays = [(inst, out_idx)]
                if comp is not None:
                    plays += comp.mix_notes(out_idx)
                for inst_i, raw_idx in plays:
                    if inst_i != inst and inst_i not in roots:
                        continue                # a source of the other chip: its own pass counts it
                    idx_i = max(0, min(35, raw_idx))
                    secs = wall
                    root = roots.get(inst_i)
                    if root is not None:
                        root_idx, shift = root
                        secs *= PERIOD_TABLE[root_idx] / PERIOD_TABLE[idx_i] / 2.0 ** (shift / 12.0)
                    if finetunes.get(inst_i, 0) > 0:
                        secs *= 2.0 ** (finetunes[inst_i] / 96.0)
                    prev = needs.get(inst_i)
                    if prev is None or secs > prev[0]:
                        needs[inst_i] = (secs, root)
        return needs

    _AUTO_SUSTAIN_CAP_SECS = 10.0

    def _resolve_sustain(self, settings, kind: str):
        """Settings with `sustain_duration: auto` resolved per instrument: each synthesised
        instrument is rendered for its own longest ring (capped at 10 s;
        `sustain_by_instrument`), `sustain_duration` itself becoming the largest of them for
        anything not measured.  A stated number renders every instrument that long.  Warns for
        every synthesised instrument whose sample cannot hold one of its notes: the setting is
        shorter, the cap is, or the MOD sample limit at the instrument's rate is
        (max_sustain_secs).  In the merged build an instrument that is not rendered
        (MergePlan.unused) sets nothing."""
        needs = self._sustain_needs(kind)
        if self._merge is not None:
            needs = {i: n for i, n in needs.items() if i not in self._merge.unused}
        auto = settings.sustain_duration == "auto"
        per_inst: dict[int, float] = {}
        if auto:
            secs = min(max((n for n, _ in needs.values()), default=0.0), self._AUTO_SUSTAIN_CAP_SECS)
            if secs <= 0:
                secs = float(type(settings)().sustain_duration)    # no notes: the field's default
            per_inst = {i: min(n, self._AUTO_SUSTAIN_CAP_SECS)
                        for i, (n, root) in needs.items() if root is not None and n > 0}
            settings = dataclasses.replace(settings, sustain_duration=secs, sustain_by_instrument=per_inst)
            self.infos.append({'type': f'auto_sustain_{kind.lower()}', 'secs': round(secs, 3),
                               'shortest': round(min(per_inst.values(), default=secs), 3),
                               'instruments': len(per_inst)})
        if not settings.enabled:
            return settings
        sustain = float(settings.sustain_duration)
        exact: set[int] = set()     # auto sustain holds every note: the sample ends where they do
        # A mix source plays inside composites too, whose rings the other chip's pass measures
        # (a PSG chime under an FM lead): its sample keeps its padding and loop
        mixed = self._merge.pcm_sources if self._merge is not None else set()
        for inst, (need, root) in sorted(needs.items()):
            if root is None:
                continue
            root_idx, shift = root
            rate = round(settings.amiga_clock / PERIOD_TABLE[root_idx] * 2.0 ** (shift / 12.0))
            fits = max_sustain_secs(rate, settings.release_padding, settings.max_sample_bytes)
            want = per_inst.get(inst, sustain)
            have = min(want, fits)
            if need <= have + 0.005:
                if inst in per_inst and inst not in self._rings_out.get(kind, ()) and inst not in mixed:
                    exact.add(inst)
                continue
            limit = ('mod' if fits < want
                     else 'cap' if auto and need > self._AUTO_SUSTAIN_CAP_SECS
                     else 'setting')
            # Held back: a sample cut to a sustain loop holds any note (_flush_sustain_warnings)
            self._pending_sustain_short[(kind, inst)] = {
                'type': 'sustain_short', 'channel': kind, 'extra_ctx': f'instrument {inst}',
                'kind': kind, 'instrument': inst, 'need': need, 'have': have,
                'rate': rate, 'limit': limit, 'max_kb': settings.max_sample_kb}
        return dataclasses.replace(settings, exact_sustain=frozenset(exact),
                                   slide_ends=frozenset(self._slide_ends.get(kind, ())))

    def _flush_sustain_warnings(self, kind: str, samples: dict) -> None:
        """Report the held-back sustain_short warnings of `kind`'s instruments, except for the
        ones whose sample now loops, and record the loops and releases found."""
        looped = []
        for inst in sorted(samples):
            loop = self._loops.get(inst)
            if loop is not None:
                pcm, rate = samples[inst]
                looped.append({'instrument': inst, 'bytes': len(pcm), 'start_ms': 1000.0 * loop.start / rate,
                               'loop_ms': 1000.0 * loop.length / rate, 'error': loop.error})
        for (k, inst), w in list(self._pending_sustain_short.items()):
            if k != kind:
                continue
            del self._pending_sustain_short[(k, inst)]
            if inst not in self._loops:
                self._add_warning(w)
        if looped:
            self.infos.append({'type': 'sustain_loops', 'kind': kind, 'looped': looped,
                               'of': len(samples), 'bytes': sum(len(p) for p, _ in samples.values()),
                               'releases': {i: r for i, r in self._release.items() if i in samples}})

    def convert(self) -> ModFile:
        """The finished MOD: the song converted, then laid out.

            passes -> pattern breaks -> loop Bxx -> trailing patterns trimmed -> (merged) narrowed
                   -> one-shots' first words zeroed (pt_zero_bytes)

        The loop's Bxx needs the post-break layout, so the order is fixed."""
        mod = self._convert_passes()
        breaks = self.config.mod_pattern_breaks or []
        if breaks:
            apply_pattern_breaks(mod, breaks)
        self._set_loop_point(breaks)

        # A break may append a blank pattern nothing reaches once the Bxx is in
        loop = next((i for i in self.infos if i['type'] == 'loop_set'), None)
        if loop:
            mod.trim_to_pattern(loop['pattern'])

        # Merged: columns every pattern leaves empty go (4 in use -> an M.K. file)
        if self.config.merge_active:
            need = ModFile.round_up_channels(max(1, mod.used_channels()))
            if need < mod.CHANNELS:
                self.infos.append({'type': 'narrowed', 'from': mod.CHANNELS, 'to': need})
                mod.narrow_to(need)

        # Silent once a one-shot ends: ProTracker replays its first word
        zero_idle = self.synth.pt_zero_bytes if self.synth else True
        if zero_idle:
            mod.zero_idle_words()
        return mod

    def _convert_passes(self) -> ModFile:
        """The song converted, as many times as the bank reserve takes (convert).

        How many instrument slots a merged build holds back for its sample banks
        (`merge_bank_slots`) is known only once the mixes are made, after the composites took
        their slots.  With `merge_bank_slots: auto` (the default) the build is made again with
        the reserve its banks turned out to need: the banks they filled, where a held-back slot
        sat empty while composites went without one; more, where bank sounds found no slot and
        they carry more notes than the least-played composites that would give theirs up.  A
        stated number is kept, except that a slot it holds back for nothing goes back to the
        composites the same way (Green Hill's slot 19 sat empty at merge_bank_slots: 3 with
        five chords lost)."""
        # The conversion edits the song and config (loop extension, spliced notes, composite
        # entries): another pass needs them as they were
        snapshot = copy.deepcopy((self.song, self.config)) if self.config.merge_active else None
        mod = self._convert_once()
        if snapshot is None or self._merge is None:
            return mod
        auto = getattr(snapshot[1], "merge_bank_slots_auto", False)
        if not auto:
            if not self._idle_bank_slots:
                return mod
            # Start over with the reserve the banks filled
            song, config = copy.deepcopy(snapshot)
            retry = {'slots': list(self._idle_bank_slots), 'reserve': config.merge_bank_slots,
                     'banks': len(self._merge.banks)}
            config.merge_bank_slots = retry['banks']
            self._start(song, config, self.synth, self.psg_synth)
            self._bank_retry = retry
            self._convert_once()
            self.infos.append({'type': 'merge_bank_retry', **retry})
            return self.mod

        tried = [self.config.merge_bank_slots]
        while True:
            want = self._bank_reserve_wanted()
            if want is None or want in tried or len(tried) >= _MAX_BANK_BUILDS:
                break
            song, config = copy.deepcopy(snapshot)
            config.merge_bank_slots = want
            self._start(song, config, self.synth, self.psg_synth)
            self._convert_once()
            tried.append(want)
        if any(c.banked for c in self._merge.composites.values()) or self._merge.banks:
            self.infos.append({'type': 'merge_bank_slots', 'reserve': self.config.merge_bank_slots,
                               'banks': len(self._merge.banks), 'passes': len(tried)})
        return self.mod

    def _bank_reserve_wanted(self) -> int | None:
        """The bank reserve this pass's banks ask for (convert, merge_bank_slots: auto), or None
        where it is right: fewer, when a held-back slot sat empty while composites went without
        one; more, when banks found no slot and their notes outnumber those of the least-played
        composites that would give up theirs."""
        plan = self._merge
        if plan is None or not (plan.banks or plan.bank_overflow):
            return None
        banks = len(plan.banks)
        if self._idle_bank_slots:
            return banks
        if not plan.bank_overflow:
            return None
        slotted = sorted(c.notes for c in plan.composites.values() if not c.banked)
        best, gain = 0, 0
        for k in range(1, len(plan.bank_overflow) + 1):
            g = sum(plan.bank_overflow[:k]) - sum(slotted[:k])
            if g > gain:
                best, gain = k, g
        return banks + best if best else None

    def _convert_once(self):
        """One conversion pass (convert)."""
        self.mod.set_name(self.config.name)

        for issue in rate3_synth_root_issues(self.config):
            self._add_warning({'type': 'rate3_synth_root', 'extra_ctx': issue['context'], **issue})

        # Every rooted entry's rendering pitch comes from the song (core.driver_state.resolve_synth_roots):
        # the chip pitch its notes play most often, or, when stated, wherever the config put it; the
        # sample's rate carries the difference from the pitch `root` sounds (synth_shift), so no note
        # moves.  Before anything reads synth_root / synth_shift.
        derived = stated = 0
        for r in resolve_synth_roots(self.song, self.config):
            derived += r['derived']
            stated += not r['derived']
            if r['shift'] and not r['derived']:
                self.infos.append({'type': 'synth_shift', **r})
            if len(r['votes']) > 1:
                self._add_warning({'type': 'synth_root_ambiguous', 'channel': 'map',
                                   'extra_ctx': r['context'], **r})
        if derived or stated:
            self.infos.append({'type': 'synth_roots', 'derived': derived, 'stated': stated})

        # Detune variants (core.detune): every smpsAlterNote detune an instrument plays gets its
        # sample rendered at that offset.  Planned on the song as written, as the audit tools
        # plan it, and before the merge plan takes the slots left free.
        self._detune = self._plan_detune()

        # Collect PSG instrument numbers that will be synthesized so disk loading
        # can skip them (avoids spurious "file not found" warnings).
        psg_synth_insts: set = set()
        if self.psg_synth and self.psg_synth.enabled:
            if self.config.psg_map:
                psg_synth_insts.update(e.mod_instrument for e in self.config.psg_map.values())
            if self.config.psg_voice_map:
                psg_synth_insts.update(
                    e.mod_instrument
                    for entries in self.config.psg_voice_map.values()
                    for e in entries
                )

        # Global duration divider changes re-time every channel (before anything reads ticks).
        # The tempo segments are collected now for the sustain scan, and again below once the
        # loop bodies are extended (a replayed body may carry a tempo change).
        for tick, div in self._apply_global_tempo_div():
            self.infos.append({'type': 'tempo_div_change', 'tick': tick, 'divider': div,
                               'row': int(tick // self._effective_tpr)})
        self._tempo_segments = self._collect_tempo_segments()

        # Extend channels whose loop body is too short to cover the full song, before anything
        # counts notes: a replayed body is as many notes as it plays (GHZ's PSG3 hi-hat is 4
        # events that become 264), and a replayed body may carry a tempo change.
        self._extend_looping_channels()
        self._tempo_segments = self._collect_tempo_segments()

        # The merged build: which composite instruments the groups need and where they play,
        # decided while the ticks are final and before anything renders (core/merge.py).
        self._merge = None
        if self.config.merge_active:
            self._merge = self._build_merge_plan()

        # Resolve 'auto' sustain durations from the longest ring each instrument plays, in the
        # MOD's own time, and warn where a sample cannot hold a note.
        synth = self._resolve_sustain(self.synth, 'FM') if self.synth else None
        psg_synth = self._resolve_sustain(self.psg_synth, 'PSG') if self.psg_synth else None

        # Load or synthesize samples (a merge composite is rendered or mixed, never loaded)
        merge_insts = (self._merge.instruments | self._merge.unused) if self._merge is not None else set()
        if synth and synth.enabled and synth.mode == "ym2612":
            generate_fm_samples = self._generators.fm
            if generate_fm_samples is None:
                raise ValueError("FM synthesis is enabled but no FM generator was given (SampleGenerators.fm)")

            # Warn about map entries whose voice index doesn't exist in the song, and
            # collect their instruments to suppress spurious "file not found" warnings.
            fm_skipped_insts: set = set()
            for _ctx, _vi, _insts in fm_catalogue(self.song, self.config).missing_voices:
                fm_skipped_insts.update(_insts)
                print(f"Warning: {_ctx} voice ${_vi:02X} not defined in song "
                      f"(inst {_insts}) — remove this entry from {_ctx.split('[')[0]}")
            # Each sample is rendered at the level most of its notes play at — the carriers carry
            # the channel volume as the driver's SetVoice writes it — so the chip clips a
            # multi-carrier voice as much as the hardware does at that level and no more.
            self._fm_render_levels = (self._plan_fm_render_levels()
                                      if self._fm_volume_mode == "baked" else {})
            fm_peaks: dict[int, tuple[int, int]] = {}
            # Sustain loops (settings.yaml `sustain_loops`): a settled voice's sample is cut to a
            # loop and its notes end with a release slide (_write_release) instead of a C00.
            fm_loops = synth.loops_for(self.config.merge_active)
            self._release_slides = fm_loops
            fm_samples = generate_fm_samples(
                self.song, self.config, synth,
                tl_offsets={inst: lv[0] for inst, lv in self._fm_render_levels.items()},
                peaks_out=fm_peaks, loops=fm_loops, loops_out=self._loops, release_out=self._release,
                raw_out=self._raw_renders)
            self._flush_sustain_warnings('FM', fm_samples)
            if self._merge is not None:
                self._scale_chip_composite_volumes(fm_peaks)
            self.infos.append({'type': 'fm_synthesized', 'count': len(fm_samples)})
            # An FM source of a pcm mix whose slot a composite holds is kept aside for the mixer
            # (as a PSG one is below); the slot's loop entry is the composite's
            fm_aside = ({i for i in fm_samples if i in self._merge.mix_only and i in self._merge.instruments}
                        if self._merge is not None else set())
            self._install_synthesized_samples({i: v for i, v in fm_samples.items() if i not in fm_aside},
                                              self.config.sample_list, "fm", synth.max_sample_bytes)
            for i in fm_aside:
                self._mix_sources[i] = self._make_sample(i, fm_samples[i][0], "fm", self._loops.pop(i, None),
                                                         synth.max_sample_bytes, original=True)
            # Load remaining (DAC) samples from disk — skip FM-synthesized and PSG-synthesized instruments
            if self.config.sample_list:
                for entry in self.config.sample_list:
                    inst_num = entry[0]
                    if (inst_num not in fm_samples and inst_num not in psg_synth_insts
                            and inst_num not in fm_skipped_insts and inst_num not in merge_insts):
                        self.mod.add_samples(self.config.samples_dir, [entry])
        elif self.config.sample_list:
            # Load all disk samples, skipping any that will be PSG-synthesized
            for entry in self.config.sample_list:
                if entry[0] not in psg_synth_insts and entry[0] not in merge_insts:
                    self.mod.add_samples(self.config.samples_dir, [entry])
        else:
            # Create placeholder samples
            max_inst = max(
                (ch.instrument for ch in self.config.channels),
                default=10
            )
            # Also include DAC instruments
            for dac in self.config.dac_samples:
                max_inst = max(max_inst, dac.mod_instrument)
            self.mod.create_placeholder_samples(max_inst)

        self._saturate_dac_samples()

        # PSG synthesis block
        if psg_synth and psg_synth.enabled and (self.config.psg_map or self.config.psg_voice_map):
            generate_psg_samples = self._generators.psg
            if generate_psg_samples is None:
                raise ValueError("PSG synthesis is enabled but no PSG generator was given (SampleGenerators.psg)")

            rate3 = self._derive_rate3_dividers()
            for inst, d in sorted(rate3.items()):
                if d['used']:
                    self.infos.append({'type': 'rate3_divider', 'instrument': inst, **d})
            noise_env = self._derive_noise_envelopes()
            for inst, d in sorted(noise_env.items()):
                if d['envelope'] is not None:
                    self.infos.append({'type': 'noise_envelope', 'instrument': inst,
                                       'envelope': d['envelope'], 'derived': d['derived'],
                                       'notes': d['counts'].get(d['envelope'], 0)})
                others = {k: n for k, n in d['counts'].items() if k != d['envelope']}
                if others:
                    self._add_warning({'type': 'noise_envelopes', 'channel': 'PSG',
                                       'extra_ctx': f'instrument {inst}', 'instrument': inst,
                                       'envelope': d['envelope'], 'others': others})
            psg_loops: dict[int, SustainLoop] = {}
            psg_samples = generate_psg_samples(
                self.config, psg_synth, rate3_dividers={i: d['n'] for i, d in rate3.items()},
                noise_envelopes={i: d['envelope'] for i, d in noise_env.items()},
                loops=psg_synth.loops_for(self.config.merge_active), loops_out=psg_loops,
                raw_out=self._raw_renders)
            # A mix-only source whose slot a composite holds is kept aside for the mixer; the
            # slot's loop entry stays the composite's
            aside = ({i for i in psg_samples if i in self._merge.mix_only and i in self._merge.instruments}
                     if self._merge is not None else set())
            self._loops.update({i: lp for i, lp in psg_loops.items() if i not in aside})
            self._flush_sustain_warnings('PSG', psg_samples)
            direct = {i: v for i, v in psg_samples.items() if i not in aside}
            self._install_synthesized_samples(direct, self.config.sample_list, "psg",
                                              psg_synth.max_sample_bytes)
            for i in aside:
                self._mix_sources[i] = self._make_sample(i, psg_samples[i][0], "psg", psg_loops.get(i),
                                                         psg_synth.max_sample_bytes, original=True)
            self.infos.append({'type': 'psg_synthesized', 'count': len(psg_samples)})

        for w in self._pending_sustain_short.values():      # kinds that were not synthesised
            self._add_warning(w)
        self._pending_sustain_short.clear()

        # The composites mixed from finished samples (DAC + hi-hat), now that every sample is in
        if self._merge is not None:
            self._mix_merge_composites(synth, psg_synth)

        # Convert channels
        self._convert_all_channels()
        if self._merge is not None and self._merge.banks:
            self.infos.append({'type': 'merge_bank_notes', 'notes': len(self._merge.regions),
                               'cuts': self._bank_cuts, 'delays_dropped': self._bank_delays_dropped,
                               'cxx_moved': self._bank_cxx_moved})
        if any(self._tie_retunes.values()):
            self.infos.append({'type': 'detune_ties', **self._tie_retunes})
        self._place_leading_rests()
        self._place_tempo_commands()
        if len(self._tempo_segments) > 1:
            self._write_tempo_changes()

        return self.mod

    def _extend_looping_channels(self):
        """Extend channels whose event data ends early due to a compact smpsJump inner loop.

        If a channel has has_jump=True and its last event tick is less than the global
        last tick across all channels, repeat the loop body (events from
        jump_target_tick onward) until channel coverage reaches global_last_tick.

        Example: PSG3 in GHZ — loop body = {NOTE nMaxPSG dur=8 at tick=48}, loop_span=8.
        Without extension: 4 events / 56 ticks. After: ~1200 events / full song.
        """
        import copy

        label_tick_pos = self.song.label_tick_pos

        # Global last tick = max(tick_position + duration) across all channels
        global_last_tick = 0
        for ch in self.song.channels:
            for ev in ch.events:
                end = ev.tick_position + (ev.note.duration if ev.note else 0)
                if end > global_last_tick:
                    global_last_tick = end

        for ch in self.song.channels:
            if not ch.has_jump or not ch.jump_target_label:
                continue
            if not ch.events:
                continue
            ch_last = max(
                ev.tick_position + (ev.note.duration if ev.note else 0)
                for ev in ch.events
            )
            if ch_last >= global_last_tick:
                continue  # Already covers full song; skip

            loop_start_tick = label_tick_pos.get(ch.jump_target_label)
            if loop_start_tick is None:
                continue

            # Loop body = the events after the jump label.  Selecting by tick alone would also
            # replay a coordination flag written just BEFORE the label at the same tick on every
            # repetition (SYZ PSG3: `smpsPSGAlterVol $FF` / `Jump03:` — the hi-hat crept from
            # attenuation 5 to 0 in five loops; on hardware it stays at 5).
            body_index = ch.label_event_index.get(ch.jump_target_label)
            if body_index is not None:
                loop_body = ch.events[body_index:]
            else:
                loop_body = [ev for ev in ch.events if ev.tick_position >= loop_start_tick]
            if not loop_body:
                continue

            # Loop span = (last body event end tick) − loop_start_tick
            last_ev = loop_body[-1]
            loop_end = last_ev.tick_position + (last_ev.note.duration if last_ev.note else 0)
            loop_span = loop_end - loop_start_tick
            if loop_span <= 0:
                continue

            # Synthesize additional iterations until we reach global_last_tick
            original_count = len(ch.events)
            offset = ch_last - loop_start_tick
            while (loop_start_tick + offset) < global_last_tick:
                for ev in loop_body:
                    new_tick = ev.tick_position + offset
                    if new_tick >= global_last_tick:
                        break
                    new_ev = copy.copy(ev)
                    new_ev.note = copy.copy(ev.note) if ev.note else None
                    new_ev.tick_position = new_tick
                    if new_ev.note:
                        cap_dur = global_last_tick - new_tick
                        new_ev.note.duration = min(new_ev.note.duration, cap_dur)
                    ch.events.append(new_ev)
                offset += loop_span

            self.infos.append({
                'type': 'loop_extended',
                'label': ch.header.label,
                'from': original_count,
                'to': len(ch.events),
                'span': loop_span,
            })

    def _sample_secs(self) -> dict[int, float]:
        """{instrument: seconds its sample lasts} for the instruments whose sample, not the
        note's duration, says how long a note is heard: the drums (their file on disk, played
        at their mod_note) and the noise instruments (their envelope, then the ramp to
        silence).  What core.merge bounds a note's sounding span with."""
        import os
        clock = self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock
        files = {e[0]: e[1] for e in (self.config.sample_list or [])}
        out: dict[int, float] = {}
        for d in self.config.dac_samples:
            path = os.path.join(self.config.samples_dir, files.get(d.mod_instrument, ""))
            note = _MOD_NOTE_MAP.get(d.mod_note)
            if d.mod_instrument in files and note is not None and os.path.exists(path):
                out[d.mod_instrument] = os.path.getsize(path) / (clock / PERIOD_TABLE[note.value])
        fps = 50.0 if self.config.region.lower() == 'pal' else 60.0
        for inst, d in self._derive_noise_envelopes().items():
            env = d['envelope']
            env = PSG_ENVELOPES_BY_NAME.get(env) if isinstance(env, str) else env
            frames = noise_envelope_frames(env if isinstance(env, list) else None)
            if frames is not None:
                out[inst] = frames / fps
        return out

    def _report_merge_groups(self) -> None:
        """One `merge_group` info per group, once the composites have their final instruments
        (after the mixes and the sample banks): the composites its notes play, with the group
        each was created for and the others that share it."""
        plan = self._merge
        assert plan is not None
        for g in plan.groups:
            if g.fill:
                continue                        # the fill pool reports it
            label = g.label + g.where
            stats = plan.stats_of(g)
            comps = []
            for c in sorted(plan.composites.values(), key=lambda c: (-c.uses.get(label, 0), c.inst, c.offset)):
                n = c.uses.get(label, 0)
                if not n:
                    continue
                slot = f"{c.inst} 9{c.offset >> 8:02X}" if c.banked else str(c.inst)
                others = [lab for lab in c.uses if lab != label]
                comps.append((slot, n, c.detail, None if c.group is g else c.group.label + c.group.where, others))
            self.infos.append({'type': 'merge_group', 'label': label, 'primary': g.primary, 'route': g.route,
                               'followers': list(g.followers),
                               'paired': sum(s.paired for s in stats),
                               'solo': sum(s.solo for s in stats),
                               'alone': stats[0].alone if stats else 0,
                               'unison': plan.unisons.get(label),
                               'composites': comps})

    def _cut_after(self, mod_chan: int, tick: int, secs: float, next_row: int) -> bool:
        """Cut the note that started at `tick` `secs` later: `C00` on the row the cut falls
        on, `ECx` inside it, unless the channel's next note-on (`next_row`, from
        _next_note_row) is there first.  Seconds are V-int frames at the region's frame rate,
        then driver ticks as a note fill is (`_tpf_at`), so a tempo change is honoured.
        Leaves the cursor on the cut's cell; returns whether one was written."""
        fps = 50.0 if self.config.region.lower() == 'pal' else 60.0
        speed = self.config.target_speed
        cut_ticks = secs * fps * self._tpf_at(tick)
        cut_abs = max(round(tick * speed / self._effective_tpr) + 1,
                      round((tick + cut_ticks) * speed / self._effective_tpr))
        row_total, sub = divmod(cut_abs, speed)
        if row_total >= next_row or row_total // 64 >= self.config.max_patterns:
            return False
        self._set_cursor(row_total // 64, mod_chan, row_total % 64)
        if sub:
            self.mod.set_effect(0xE, 0xC0 | sub)
        else:
            self.mod.set_effect(0xC, 0)
        return True

    def _pattern_of_tick(self, tick: int) -> int:
        """The pattern of the reference build (after its `mod_pattern_breaks`) a note-on at
        `tick` lands in — what a `merge_patterns:` group is matched on (core.merge)."""
        return _shift_for_breaks(int(tick // self._effective_tpr), self.config.mod_pattern_breaks or []) // 64

    def _last_pattern(self) -> int:
        """The MOD's last pattern: the one the loop's `Bxx` row lands in (`_set_loop_point`);
        what convert.py trims the output to.  A loop extension may overshoot the song's end by a
        tick, and that note lands in a pattern nothing reaches."""
        tpr = self._effective_tpr
        end = max((ev.tick_position + (ev.note.duration if ev.note else 0)
                   for ch in self.song.channels for ev in ch.events), default=0)
        row = max(round(end / tpr), 1) - 1
        return _shift_for_breaks(row, self.config.mod_pattern_breaks or []) // 64

    def _build_merge_plan(self):
        """core.merge.build_merge_plan with this conversion's pan law and baked levels, its
        findings reported as infos / warnings."""
        self._merge_pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
        self._merge_baselines = {}
        if self._fm_volume_mode == "baked":
            self._merge_baselines["FM"] = self._plan_levels("FM")
        if self._psg_volume_mode == "baked":
            self._merge_baselines["PSG"] = self._plan_levels("PSG")
        plan = build_merge_plan(self.song, self.config, pan_law_db=self._merge_pan_law,
                                baselines=self._merge_baselines, sample_secs=self._sample_secs(),
                                tick_secs=lambda t: self._tick_span_secs(t, t + 1),
                                fill_min_ticks=math.ceil(self._effective_tpr),
                                pattern_of=self._pattern_of_tick, last_pattern=self._last_pattern())
        for src, d in sorted(plan.dropped_notes.items()):
            self._add_warning({'type': 'merge_dropped', 'channel': src, 'notes': d['notes'],
                               'patterns': sorted(d['patterns'])})
        if plan.unspecified:
            self._add_warning({'type': 'merge_unspecified', 'channel': 'merge',
                               'patterns': sorted(plan.unspecified)})
        for f in plan.fill:
            self.infos.append({'type': 'merge_fill', **f})
            if f['lost']:
                self._add_warning({'type': 'merge_fill_lost', 'channel': f['source'], **f})
        if plan.unused:
            self.infos.append({'type': 'merge_unused', 'instruments': sorted(plan.unused)})
        self.infos.append({'type': 'merge_slots', 'free': plan.slots_free, 'wanted': plan.slots_wanted,
                           'used': sum(1 for c in plan.composites.values() if not c.banked),
                           'banked': sum(1 for c in plan.composites.values() if c.banked),
                           'stand_ins': sum(1 for u in plan.unsupported
                                            if u.get('stand_in') and u['reason'] == NO_SLOT)})
        for s in plan.stats:
            where = s.group.where if s.group is not None else ""
            if s.lost or s.vibrato:
                self._add_warning({'type': 'merge_lost', 'channel': f"{s.primary}+{s.follower}{where}",
                                   'primary': s.primary, 'where': where,
                                   'follower': s.follower, 'held': s.held, 'shorter': 0,
                                   'truncated': s.truncated, 'orphans': s.orphans, 'solo_cut': s.solo_cut,
                                   'cuts': 0, 'vibrato': s.vibrato, 'notes': s.follower_notes})
            # Working as configured, not a loss: a note keyed off early inside its composite, a
            # note that cuts the primary's tail (cut_primary) - one dim line, not a warning
            parts = []
            if s.shorter:
                parts.append(f"{s.shorter} keyed off early inside the composite")
            if s.cuts:
                parts.append(f"{s.cuts} cut the primary's tail (cut_primary)")
            if parts:
                self.infos.append({'type': 'merge_folds', 'pair': f"{s.primary}+{s.follower}{where}",
                                   'what': f"of {s.follower}'s {s.follower_notes} notes: " + "; ".join(parts)})
        # Composites without a slot, one warning per primary (a stand-in is no loss)
        by_primary: dict[str, dict] = {}
        for u in plan.unsupported:
            d = by_primary.setdefault(u['primary'], {'count': 0, 'notes': 0, 'stand_ins': 0, 'details': [],
                                                     'reasons': set()})
            if u.get('stand_in'):
                d['stand_ins'] += 1
                continue
            d['count'] += 1
            d['notes'] += u['notes']
            d['reasons'].add(u['reason'])
            d['details'].append(f"{u['notes']} notes: {u['detail']}")
        for primary, d in by_primary.items():
            if d['count']:
                self._add_warning({'type': 'merge_unsupported', 'channel': primary, 'primary': primary,
                                   'count': d['count'], 'notes': d['notes'], 'stand_ins': d['stand_ins'],
                                   'reason': " / ".join(sorted(d['reasons'])), 'details': d['details']})
        return plan

    def _derive_noise_envelopes(self) -> dict[int, dict]:
        """The envelope each noise instrument plays with, read from the song.

        smpsPSGform only switches the channel to noise; the envelope is the driver's VoiceIndex
        — the header voice (fTone_04 for sixteen of the eighteen PSG3 tracks, fTone_09 in Marble
        Zone, fTone_08 in Robotnik) or the last smpsPSGvoice.  Walking the PSG channels with the
        DriverState, every noise note votes for the label in force; an instrument's envelope is
        the label most of its notes play with (ties to the first heard).  A psg_map entry's
        `envelopes:` variants are their own instruments and carry their label.  A stated
        `envelope:` on the entry overrides the vote.

        Returns {instrument: {'envelope', 'derived', 'counts': {label: notes}}}; `counts` lists
        every label the instrument played with, so the caller can warn where one sample stands
        in for several envelopes (Credits' PSG3, which has no free slot for variants).
        """
        counts: dict[int, dict[str | None, int]] = {}
        for chan_cfg, channel in enabled_channels(self.song, self.config, ("PSG",)):
            for _event, st, res in walk_channel(channel, self.config, chan_cfg):
                if res is not None and st.in_noise_mode and st.psg_entry is not None:
                    per = counts.setdefault(st.instrument, {})
                    per[st.envelope] = per.get(st.envelope, 0) + 1

        out: dict[int, dict] = {}
        for entry in self.config.psg_map.values():
            base_counts = counts.get(entry.mod_instrument, {})
            if entry.envelope is not None:
                envelope, derived = entry.envelope, False
            elif base_counts:
                envelope, derived = max(base_counts, key=lambda k: base_counts[k]), True
            else:
                envelope, derived = None, True
            out[entry.mod_instrument] = {'envelope': envelope, 'derived': derived, 'counts': base_counts}
            for label, inst in entry.envelopes.items():
                out[inst] = {'envelope': label, 'derived': True, 'counts': counts.get(inst, {})}
        return out

    def _derive_rate3_dividers(self) -> dict[int, dict]:
        """Tone-2 divider the driver writes for each rate-3 (`noise_rate: 3`) noise instrument.

        In rate-3 mode the LFSR is clocked by tone channel 2, and the Sonic 1 driver keeps writing
        PSG3's own note there: divider = PSGFrequencies[note − $81 + transpose].  So the right
        divider is in the song data, not something a config has to state: the hi-hat's `nMaxPSG`
        is table entry 69 = divider 0, which the Sega VDP PSG clocks as 1 (near-white hiss), and
        Marble Zone's pitched noise is whatever its notes say.

        The divider is taken at the entry's `low` note when it has one (the sample plays at `root`
        for that note, and MOD playback speed moves it from there), otherwise from the note the
        instrument plays most.  Returns {instrument: {'n', 'note', 'transpose', 'used'}};
        `used` is False when the config states `tone2_n` or `synth_root`, which win.
        """

        seen: dict[int, dict] = {}        # instrument -> {'entry', 'notes': {(note_value, transpose): count}}
        for chan_cfg, channel in enabled_channels(self.song, self.config, ("PSG",)):
            for event, st, res in walk_channel(channel, self.config, chan_cfg):
                if res is None:
                    continue
                e = res.entry
                if e is not None and e.type != "tone" and e.noise_rate == 3:
                    # An envelope variant (psg_map entry's `envelopes:`) is its own instrument
                    rec = seen.setdefault(res.instrument, {'entry': e, 'notes': {}})
                    key = (event.note.note_value, st.transpose)
                    rec['notes'][key] = rec['notes'].get(key, 0) + 1

        out: dict[int, dict] = {}
        for inst, rec in seen.items():
            e, notes = rec['entry'], rec['notes']
            at_low = {k: c for k, c in notes.items() if e.low is not None and k[0] - 0x81 == e.low}
            if at_low:
                note_value, transpose = max(at_low, key=lambda k: at_low[k])
            else:
                note_value, transpose = max(notes, key=lambda k: notes[k])
                if e.low is not None:                       # anchor never played: same transpose, the anchor note
                    note_value = e.low + 0x81
            n = psg_tone2_divider(note_value, transpose)
            out[inst] = {'n': n, 'note': _semitone_to_name(note_value - 0x81), 'transpose': transpose,
                         'used': e.tone2_n is None and e.synth_root is None}
        return out

    def _plan_detune(self) -> DetunePlan | None:
        """The detune variants (core.detune) where FM is synthesised and settings allow them."""
        if not detune_variants_wanted(self.synth):
            return None
        plan = plan_detune_variants(self.song, self.config)
        if plan.own or plan.variants:
            self.infos.append({'type': 'detune_variants', 'own': dict(plan.own),
                               'variants': [(v.inst, v.base, v.detune, v.notes) for v in plan.variants.values()]})
        if plan.unplaced:
            self._add_warning({'type': 'detune_no_slot', 'channel': 'FM', 'unplaced': dict(plan.unplaced)})
        return plan

    def _sample_cents(self, inst: int) -> float:
        """Cents an FM instrument's sample is detuned by (core.detune): its FNUM offset at the
        pitch it is rendered at."""
        if self._sample_detunes is None:
            self._sample_detunes = {i.inst: detune_cents(i.synth_idx + 12, i.layers[0].fnum_offset)
                                    for i in fm_catalogue(self.song, self.config).instruments.values()
                                    if len(i.layers) == 1}
        return self._sample_detunes.get(inst, 0.0)

    def _fine_slide(self, pattern: int, row: int, mod_chan: int, period: int, cents: float) -> int | None:
        """E1x / E2x moving a sounding note `cents` (up: positive) from `period`, on a cell with
        no note and a free effect slot → the period units it moved (up: positive), else None."""
        units = round(period - period * 2.0 ** (-cents / 1200.0))
        units = max(-_FINE_SLIDE_MAX, min(_FINE_SLIDE_MAX, units))
        if not units:
            return None
        if self.mod.note_at(pattern, row, mod_chan) or not self.mod.effect_slot_free(pattern, row, mod_chan):
            self._tie_retunes['skipped'] += 1
            return None
        self._set_cursor(pattern, mod_chan, row)
        self.mod.set_effect(0xE, (0x10 if units > 0 else 0x20) | abs(units))
        self._tie_retunes['placed'] += 1
        return units

    def _count_levels(self, kind: str, level_of, sources_keep_votes: bool = False) -> dict[int, dict]:
        """{MOD instrument: {level_of(state, resolved): notes}} over every enabled channel of
        `kind` ("FM" or "PSG"), walked with the same DriverState the conversion uses.  A PSG
        note at attenuation 15 is silent and does not vote.  The instruments a merged build's
        unison chords play louder (ResolvedNote.gain_db) are noted in `_gained`.

        In a merged build a composite's slot counts only the notes that play the composite.
        `sources_keep_votes`: except where the slot's former instrument is a mix source kept
        aside under that number (MergePlan.mix_only) - it is still rendered, at its own notes'
        level, for the mixer (the render levels ask for that).  A detune variant (core.detune)
        votes as its base instrument: it is that sample a few cents off.
        """
        counts: dict[int, dict] = {}
        plan = self._merge
        owned = set(plan.instruments) if plan is not None else set()
        if plan is not None and sources_keep_votes:
            owned -= plan.mix_only
        for chan_cfg, channel in enabled_channels(self.song, self.config, (kind,)):
            for event, st, res in walk_channel(channel, self.config, chan_cfg):
                if res is None or (st.is_psg and st.is_silent):
                    continue
                # A composite owns its slot: a note that resolves to the slot's former instrument
                # (a follower's folded note, a dropped channel's) does not vote for the composite's
                # level.  Green Hill's FM4 voice $07 notes set the level of the FM2+PSG1 chord that
                # had taken slot 13, and its one note got a C40.
                if (plan is not None and res.instrument in owned
                        and (chan_cfg.source, event.tick_position) not in plan.ticks):
                    continue
                inst = self._detune.base_of(res.instrument) if self._detune else res.instrument
                if plan is not None:
                    member = plan.bank_members.get((chan_cfg.source, event.tick_position))
                    if member is not None:
                        inst = member.bank_id       # the bank's slot holds other sounds' levels too
                per = counts.setdefault(inst, {})
                k = level_of(st, res)
                per[k] = per.get(k, 0) + 1
                if res.gain_db:
                    self._gained.setdefault(kind, set()).add(inst)
        return counts

    def _plan_levels(self, kind: str) -> dict[int, float]:
        """"baked" volume mode: the level (dB) each MOD instrument's sample_list volume stands for.

        The level with the most notes is the instrument's baseline — those notes need no Cxx.
        Ties go to the louder level so the others are attenuated rather than boosted past 64.

        FM levels come from the TL offset (smpsHeaderFM volume + smpsAlterVol) and the pan;
        PSG levels from the attenuation (smpsHeaderPSG volume + smpsPSGAlterVol).
        """
        pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
        counts = self._count_levels(kind, lambda st, res: st.level_db(pan_law) + res.gain_db)
        return self._with_variants({inst: modal_level(levels) for inst, levels in counts.items()})

    def _plan_fm_render_levels(self) -> dict[int, tuple[int, bool]]:
        """{MOD instrument: (carrier TL offset, hard-panned)} its FM sample is rendered at.

        The level most of the instrument's notes play at, chosen as _plan_levels chooses its
        baseline (ties to the louder), so the sample carries the level its sample_list volume
        stands for.  The driver adds the track volume to the carrier TLs before the chip sums
        them (SetVoice), so rendering at that offset clips a multi-carrier voice exactly as
        much as the hardware does at that level — at TL 0 every GHZ lead clipped a third of
        its samples where the hardware, at the channel's +18 TL, clips none.
        """
        pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
        counts = self._count_levels("FM", lambda st, _res: (st.tl, st.hard_panned), sources_keep_votes=True)
        return self._with_variants({inst: max(per, key=lambda k: (per[k], fm_level_db(k[0], k[1], pan_law)))
                                    for inst, per in counts.items()})

    def _with_variants(self, per_inst: dict) -> dict:
        """A per-instrument plan with every detune variant given its base's value."""
        return self._detune.share_base(per_inst) if self._detune else per_inst

    def _convert_all_channels(self):
        """Convert all SMPS channels to MOD channels."""
        # Source names follow header order: DAC (if present), then FM1..FMn, then PSG1..PSGn
        source_map = source_map_for(self.song)

        self._fm_baseline_db: dict[int, float] = {}
        if self._fm_volume_mode == "baked":
            self._fm_baseline_db = self._plan_levels("FM")
            # The samples were rendered (before the loop bodies were extended) at what this
            # walk now says is each instrument's baseline; the two must agree or the Cxx law
            # would be measured from a level the sample does not carry.
            pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
            for inst, (tl, pan) in getattr(self, '_fm_render_levels', {}).items():
                base = self._fm_baseline_db.get(inst)
                if inst in self._gained.get("FM", ()):
                    continue            # a unison chord's gain is in the baseline, not in the render
                if self._merge is not None and inst in self._merge.mix_only and inst in self._merge.instruments:
                    continue            # rendered for the mixer; the slot's baseline is its composite's
                if base is not None and abs(fm_level_db(tl, pan, pan_law) - base) > 1e-9:
                    print(f"Warning: instrument {inst} was rendered at TL +{tl}"
                          f"{' panned' if pan else ''} ({fm_level_db(tl, pan, pan_law):+.2f} dB) but its "
                          f"baked level is {base:+.2f} dB — the loop extension changed the modal level")
        self._psg_baseline_db: dict[int, float] = {}
        if self._psg_volume_mode == "baked":
            self._psg_baseline_db = self._plan_levels("PSG")

        # Merged build: volumes measured for the reference build, moved to the merged build's levels
        if self._merge is not None:
            self._bake_chip_composite_volumes()
            self._bake_unison_volumes()

        # Convert each configured channel
        for chan_cfg in self.config.channels:
            if not chan_cfg.enabled:
                continue

            source = chan_cfg.source
            if source not in source_map:
                self._add_warning({'type': 'missing_source', 'source': source})
                continue

            smps_channel = source_map[source]
            is_dac = (source == "DAC")

            self._convert_channel(smps_channel, chan_cfg, is_dac)

    # --- merged build: mixes and banks ---------------------------------------------------------

    def _mix_merge_composites(self, synth, psg_synth) -> None:
        """The pcm composites mixed, the banked ones packed, the groups reported.  `synth` /
        `psg_synth` are the settings with their sustain resolved."""
        plan = self._merge
        assert plan is not None
        clock = self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock
        max_bytes = self.synth.max_sample_bytes if self.synth else MAX_MOD_SAMPLE_BYTES
        hold = {}
        for s in (synth, psg_synth):
            if s is not None:
                hold.update({i: n + s.release_padding for i, n in s.sustain_by_instrument.items()})

        banked: dict[int, ModSample] = {}
        mix_raw: dict[int, list[float]] = {}
        for p in mix_pcm_composites(plan, self.mod, clock, max_bytes, hold_secs=hold,
                                    sources=self._mix_sources, release_db_s=self._release,
                                    bank_out=banked, raw=self._raw_renders, raw_out=mix_raw,
                                    padding_secs=(synth.release_padding if synth else 0.0),
                                    loop_drift_db=(synth.loop_drift_db if synth else FLAT_DB),
                                    taps=(synth.resample_taps if synth else DEFAULT_TAPS),
                                    shelf_hz=(synth.treble_shelf_hz if synth else DEFAULT_SHELF_HZ),
                                    dither=self._dither, entry_dithers=self._entry_dithers()):
            self._add_warning({'type': 'merge_missing_sample', 'channel': 'merge', **p})
        if banked:
            self._pack_merge_banks(banked, mix_raw, clock, max_bytes)
        self._report_merge_groups()

        # A mixed composite ends the way its primary does (the release slide's rate).  A banked
        # one shares its slot with sounds of other primaries (a drum hit, a bass note): its notes
        # look their primary's rate up themselves (_convert_channel, `bank_member`)
        for c in plan.composites.values():
            if c.fm is None and not c.banked and c.primary in self._release:
                self._release.setdefault(c.inst, self._release[c.primary])

        limited = [c for c in plan.composites.values() if c.limited_db > 0]
        if limited:
            self.infos.append({'type': 'merge_limited', 'composites': len(limited),
                               'max_db': max(c.limited_db for c in limited)})
        over = sorted((c.inst, c.headroom_db) for c in plan.composites.values() if c.headroom_db > _HEADROOM_REPORT_DB)
        if over:
            self._add_warning({'type': 'merge_headroom', 'channel': 'merge', 'instruments': over})

    def _pack_merge_banks(self, banked: dict[int, ModSample], mix_raw: dict[int, list[float]],
                          clock: float, max_bytes: int) -> None:
        """core.banks packs the banked mixes; a reserved slot left empty while composites lost
        theirs is noted for convert()'s second pass."""
        plan = self._merge
        assert plan is not None

        # The banks' silence after each sound covers the cut's rounding: one MOD tick, at the
        # slowest tempo the song plays
        segments = self._tempo_segments or [(0, self.song.header.tempo_modifier)]
        tick_secs = max(2.5 / self._bpm_for(m) for _, m in segments)
        for p in pack_banks(plan, self.config, self.mod, banked, plan.spare_slots, max_bytes, tick_secs, clock,
                            raw=mix_raw, dither=self._dither, entry_dithers=self._entry_dithers()):
            self._add_warning({'type': 'merge_bank_dropped', 'channel': p['primary'], 'extra_ctx': p['detail'], **p})
        for b in plan.banks:
            self.infos.append({'type': 'merge_bank', 'slot': b.slot, 'bytes': b.bytes, 'volume': b.volume,
                               'members': [(c.offset, c.region, c.notes, c.detail) for c in b.members]})

        idle = [s for s in plan.spare_slots if s not in {b.slot for b in plan.banks}]
        # Only a composite the fit had no slot for could have used one: a budget's or a twin's
        # drop (max_composites, merge_twins) is the config's choice
        dropped = sum(1 for u in plan.unsupported if not u.get('stand_in') and u['reason'] == NO_SLOT)
        if not (idle and dropped):
            return
        self._idle_bank_slots = idle
        if self._bank_retry is not None:            # the second pass left one idle too
            self._add_warning({'type': 'merge_bank_idle', 'channel': 'merge', 'slots': idle,
                               'reserve': self.config.merge_bank_slots, 'banks': len(plan.banks),
                               'dropped': dropped})

    # --- merged build: sample volumes ----------------------------------------------------------
    #
    # A composite's volume (its sample_list entry), in pipeline order:
    #
    #   plan    the primary's own volume                          core.merge.build_merge_plan
    #   chip    x peak(all layers) / peak(primary layer)          _scale_chip_composite_volumes
    #   chip    moved to the level its own notes play most        _bake_chip_composite_volumes
    #   mix     the normalised sum's level                        core.merge.mix_pcm_composites
    #   bank    the loudest member's; the others scaled in bytes  core.banks.pack_banks
    #
    # A unison chord's primary instrument moves too (_bake_unison_volumes).

    def _set_sample_volume(self, entry: list, volume: int) -> None:
        """A sample's volume, in both places it is kept: its sample_list entry and the MOD."""
        if len(entry) > 2:
            entry[2] = volume
        else:
            entry.append(volume)
        sample = self.mod.samples[entry[0] - 1]
        if sample is not None:
            sample.set_volume(volume)

    def _scale_chip_composite_volumes(self, fm_peaks: dict[int, tuple[int, int]]) -> None:
        """A chip composite is normalised like any sample: its volume is the primary's times the
        composite's peak over its primary layer's, so the primary plays as loud as it did and the
        followers add to it as the hardware sum did.  Set before the samples are installed."""
        assert self._merge is not None
        for c in self._merge.composites.values():
            if c.fm is None or c.entry is None or c.inst not in fm_peaks:
                continue
            pk_all, pk_first = fm_peaks[c.inst]
            if not pk_first:
                continue
            vol = c.entry[2] * pk_all / pk_first
            if vol > MOD_MAX_VOLUME:
                c.headroom_db = headroom_db(vol)
            c.entry[2] = clamp_mod_volume(vol)

    def _bake_chip_composite_volumes(self) -> None:
        """A chip composite's volume stands for its primary's baked level in the REFERENCE build
        (what the volume was measured for); its notes play most at their own.  Move it by the
        difference.  The merged build's own baseline for the primary would not do: Green Hill's
        bell arp dropped 13.5 dB once its voice kept a few fallback notes at another level."""
        assert self._merge is not None
        for c in self._merge.composites.values():
            if c.fm is None or c.entry is None:
                continue
            base_p = self._merge_baselines.get("FM", {}).get(c.primary)
            base_c = self._fm_baseline_db.get(c.inst)
            if base_p is None or base_c is None:
                continue
            self._set_sample_volume(c.entry, db_to_mod_volume(c.entry[2], base_c - base_p))

    def _bake_unison_volumes(self) -> None:
        """An instrument unison chords play louder (core.merge.unison_gain_db) is baked at the
        level most of its notes now play, gain included; its volume moves from the reference
        build's by the difference.  No Cxx: every Green Hill FM4+FM5 unison starts between rows,
        and a Cxx there lands a row late, after an attack at the old level."""
        assert self._merge is not None
        over = []
        for kind, baseline in (("FM", self._fm_baseline_db), ("PSG", self._psg_baseline_db)):
            ref = self._merge_baselines.get(kind, {})
            for inst in sorted(self._gained.get(kind, ())):
                if inst in self._merge.instruments or inst not in ref or inst not in baseline:
                    continue
                db = baseline[inst] - ref[inst]
                for e in self.config.sample_list or []:
                    if e[0] != inst:
                        continue
                    was = e[2] if len(e) > 2 else MOD_MAX_VOLUME
                    want = was * 10 ** (db / 20.0)
                    if want > MOD_MAX_VOLUME:
                        over.append((inst, headroom_db(want)))
                    vol = db_to_mod_volume(was, db)
                    self._set_sample_volume(e, vol)
                    self.infos.append({'type': 'merge_unison_volume', 'instrument': inst, 'volume': vol, 'db': db})
        if over:
            self._add_warning({'type': 'merge_headroom', 'channel': 'merge unison', 'instruments': over})

    def _convert_channel(self, channel: SmpsChannel, chan_cfg: ChannelConfig, is_dac: bool):
        """Convert a single SMPS channel to MOD data."""
        mod_chan = chan_cfg.mod_channel
        router = _ColumnRouter(self, chan_cfg.source, mod_chan)

        # Driver state: level, pan, transpose, FM voice and the active PSG entry — including
        # the smpsHeaderPSG voice.  Shared with the level pre-passes and the rate-3 derivation
        # so the four passes cannot disagree about what a note plays.
        st = DriverState.for_channel(channel, self.config, chan_cfg.instrument)

        # MOD-emission state, which the driver knows nothing about
        current_volume = chan_cfg.volume
        note_fill = 0
        vibrato_active = False
        vibrato_speed = 0
        vibrato_change = 0   # raw SMPS delta byte (FNUM / PSG divider units); scaled per note
        vibrato_steps = 0    # raw SMPS steps byte
        vibrato_wait = 0   # ticks to delay before vibrato starts
        active_range_entry = None  # voice_map InstrumentRange matched on most recent note

        # Build DAC name -> config map
        dac_map = {}
        for dac_cfg in self.config.dac_samples:
            dac_map[dac_cfg.name] = dac_cfg

        # Build {inst_num: sample_vol} from sample_list for Cxx scaling
        _sample_vol_map = {}
        if self.config.sample_list:
            for sl_entry in self.config.sample_list:
                _sample_vol_map[sl_entry[0]] = sl_entry[2] if len(sl_entry) > 2 else 64

        # PSG auto note-cut: hardware PSGDoNext sets vol=15 when note duration expires.
        # Pre-collect note-on (pattern, row) positions so we don't place a C00 where
        # a subsequent set_note call would write a note (set_note retains effect bytes,
        # so a pre-placed C00 would silence the next note trigger).
        is_psg = chan_cfg.source.startswith('PSG')

        # How the level st tracks reaches the MOD — see SynthesisSettings.fm_volume_mode.
        # "baked" needs no accumulator (the note's level is read off st at placement time);
        # the other two carry current_volume, a MOD volume in its own right.
        _fm_mode = self._fm_volume_mode
        _fm_vol_scaling = _fm_mode == "absolute"
        # The FM law applies to every FM note on this channel, its own or one spliced in from
        # another channel (core.merge: a solo or pool note on the drum channel keeps its level)
        _fm_baked = _fm_mode == "baked"
        _psg_baked = is_psg and self._psg_volume_mode == "baked"
        _pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
        if is_dac or (not is_psg and _fm_mode == "off"):
            st.tl = 0          # neither mode reads the smpsHeaderFM volume byte
        if is_psg:
            current_volume = round(psg_att_to_mod(st.att) * chan_cfg.volume / 64)
        elif _fm_vol_scaling and not is_dac:
            current_volume = round(fm_tl_to_mod(st.tl) * chan_cfg.volume / 64)

        _psg_mode_baked = self._psg_volume_mode == "baked"

        # The sounding note, as the closures below read it (set per note in the loop):
        # dB a unison chord adds to it (ResolvedNote.gain_db), and the banked composite it plays
        # (core.banks, else None) - measured under its own id, released at its primary's rate
        note_gain = 0.0
        bank_member: Composite | None = None

        def _emit_volume(inst: int) -> int:
            """MOD volume for a note on `inst` right now (equals the sample volume → no Cxx).

            Read off `st`, which is this channel's state or, for a follower's solo note on a
            merged channel, the follower's (so a PSG hat on the drum channel keeps its law).
            """
            sv = _sample_vol_map.get(inst, 64)
            baked = _psg_mode_baked if st.is_psg else _fm_baked
            if not baked:
                return round(current_volume * sv / 64)
            if st.is_psg and st.is_silent:
                return 0
            level = st.level_db(_pan_law) + note_gain
            baseline = self._psg_baseline_db if st.is_psg else self._fm_baseline_db
            key = bank_member.bank_id if bank_member is not None and bank_member.inst == inst else inst
            rel_db = level - baseline.get(key, level)
            # A banked sound's bytes carry its own volume against the bank's (sv): at its own
            # level it needs no Cxx either
            return clamp_mod_volume(sv * 10 ** (rel_db / 20.0) * chan_cfg.volume / 64)

        # How long each note-on rings before a PSG duration cut: its own duration plus the
        # smpsNoAttack continuations after it (`nE5, $34, smpsNoAttack, $34` holds 104 ticks on
        # the hardware; Green Hill's last verse chord cut its chime at 52)
        _ring_ticks: dict[int, int] = {}
        _ringing = None
        for _ev in channel.events:
            if not _ev.is_note:
                continue
            if _ev.note.is_rest and _ev.note.is_no_attack and _ringing is not None:
                _ring_ticks[id(_ringing)] += _ev.note.duration
                continue
            _ringing = None if _ev.note.is_rest else _ev
            if _ringing is not None:
                _ring_ticks[id(_ev)] = _ev.note.duration

        _note_on_positions: set[tuple[int, int]] = set()
        if is_psg:
            for _ev in channel.events:
                if _ev.is_note and not _ev.note.is_rest and router.plays_here(_ev):
                    _note_on_positions.add(self._tick_to_pattern_row(_ev.tick_position))
                    _note_on_positions.add(divmod(int(_ev.tick_position // self._effective_tpr), 64))

        last_note_cell: tuple[int, int] | None = None   # where this channel's previous note-on went
        last_inst: int | None = None                    # the instrument the previous note-on played ...
        last_vol = 64                                   # ... and the MOD volume it played at
        last_idx = 0                                    # ... its MOD note ...
        last_chip: int | None = None                    # ... and the chip pitch that note sounds
        last_voice: int | None = None                   # ... and the FM voice it was played with
        # The detune a tie (smpsNoAttack + a duration) sounds at: the driver writes the frequency
        # with the track's Detune on a tie too, so Scrap Brain FM4's scoop (`smpsAlterNote $EC`,
        # `nG5, $02`, `smpsAlterNote $00`, `smpsNoAttack, $06`) rises 49 c on its tie.  The MOD
        # note keeps its detune variant's sample there: an E1x / E2x moves its period instead.
        sounding_cents: float | None = None             # cents the sounding FM note plays off its table pitch
        sounding_period = 0                             # ... and its MOD period, slides included

        def _retune_tie(tick: int) -> None:
            nonlocal sounding_cents, sounding_period
            assert sounding_cents is not None and last_chip is not None
            pattern, row = self._tick_to_pattern_row(tick)
            mod_chan = router.current(tick)
            if pattern >= self.config.max_patterns:
                return
            if (pattern, row) == last_note_cell or router.borrowed(mod_chan, tick):
                return                      # the attack row's slide would retune the attack too
            self.mod.ensure_pattern(pattern)
            want = detune_cents(last_chip, st.detune) - sounding_cents
            moved = self._fine_slide(pattern, row, mod_chan, sounding_period, want)
            if moved is not None:
                sounding_period -= moved
                sounding_cents += 1200.0 * math.log2((sounding_period + moved) / sounding_period)
        # Every note-on tick of this channel (spliced notes included): a release slide runs up to
        # the row before the next one, so it never lands in a note-on's cell
        _note_on_ticks = sorted(ev.tick_position for ev in channel.events
                                if ev.is_note and not ev.note.is_rest and router.plays_here(ev))

        def _next_note_row(tick: int) -> int:
            """The first row (over the whole song) the next note-on after `tick` can land on."""
            i = bisect.bisect_right(_note_on_ticks, tick)
            if i >= len(_note_on_ticks):
                return self.config.max_patterns * 64
            return int(_note_on_ticks[i] // self._effective_tpr)

        def _release_rows(inst: int | None, tick: int) -> float | None:
            """How the instrument's release reaches the MOD at `tick`: None for a cut (C00 /
            ECx, as the hardware's instant release or a sample that has nothing to release),
            else its rate in dB per second for _write_release."""
            if not self._release_slides or inst is None:
                return None
            if bank_member is not None and bank_member.inst == inst:
                rate = self._release.get(bank_member.primary)   # its own sound's, not the bank slot's
            else:
                rate = self._release.get(inst)
            if rate is None or rate == math.inf:
                return None
            row_secs = self.config.target_speed * 2.5 / self._bpm_for(self._segment_at(tick)[1])
            if self.config.target_speed < 2 or (rate > 0 and 30.0 / rate < row_secs):
                return None                 # over within a row: a cut is closer than a slide
            return rate

        def _note_cell(tick: int, slot_free: bool, cut_tick: float | None) -> tuple[int, int, int]:
            """(pattern, row, EDx delay in MOD ticks) for a note-on at `tick`.

            A note that starts between two rows goes on the row it starts in, delayed by `EDx`,
            instead of being rounded to the nearer row (up to half a row early or late, and —
            Python rounds halves to even — early and late on alternate notes).  The delay needs
            the cell's one effect slot, so it is only used when the slot is free (`slot_free`:
            no `Cxx` due on the attack row that cannot move to a later row of the note) and no
            cut (`cut_tick`) falls inside the attack row.  Otherwise the note is rounded as
            before.

            Two note-ons cannot share a cell.  When the row already holds this channel's
            previous note-on (a 1-tick grace note and the note it slides into), the later one
            takes the next row undelayed: late by less than a row instead of erasing the grace.
            """
            tpr, speed = self._effective_tpr, self.config.target_speed
            row_total = int(tick // tpr)
            # The delay is measured in FRAMES, because driver ticks are not evenly spaced: with
            # tempo modifier m, TempoWait holds every m-th frame, so tick k falls on frame
            # k + k // (m - 1).  GHZ (m = 3, 2 ticks per row): an odd tick is 1 frame = 16.7 ms
            # after its row starts, not the 25 ms an average tick lasts - exactly ED1 at speed 3.
            # A row is tpr / _tpf(m) frames and `speed` MOD ticks long.
            # (Counted from the start of the current tempo segment: smpsSetTempoMod restarts
            # the counter.)
            seg_start, m = self._segment_at(tick)
            holds = m > 1 and not self.song.header.is_sfx

            def held(k):
                return max(int(k) - seg_start, 0) // (m - 1) if holds else 0
            frames = (tick + held(tick)) - (row_total * tpr + held(row_total * tpr))
            delay = int(frames * speed * self._tpf(m) / tpr + 0.5)
            if delay >= speed:
                row_total, delay = row_total + 1, 0
            if delay and cut_tick is not None and round(cut_tick * speed / tpr) < (row_total + 1) * speed:
                slot_free = False
            if delay and not slot_free:
                row_total, delay = round(tick / tpr), 0
            if divmod(row_total, 64) == last_note_cell:
                row_total, delay = row_total + 1, 0
            return row_total // 64, row_total % 64, delay

        for event, st_ev, res in walk_channel(channel, self.config, chan_cfg, st):
            # A follower's solo note on a merged channel arrives with the follower's state
            # (core.merge); every other event with this channel's own.
            st = st_ev
            if event.is_effect:
                # walk_channel has advanced st past this flag: level, pan, transpose, FM voice
                # and PSG instrument routing.  What is left is MOD-emission state.
                eff = event.effect

                if eff.effect_type == 'smpsAlterVol':
                    # st.apply moved the TL offset / attenuation; the non-baked modes keep
                    # their own MOD-volume accumulator on top of it.
                    if is_psg:
                        current_volume = round(psg_att_to_mod(st.att) * chan_cfg.volume / 64)
                    elif _fm_vol_scaling:
                        current_volume = round(fm_tl_to_mod(st.tl) * chan_cfg.volume / 64)
                    elif not _fm_baked:
                        current_volume = max(0, min(64, current_volume - eff.params[0]))

                elif eff.effect_type == 'smpsAlterNote':
                    pass  # raw FNUM offset (~10 cents); does not affect note pitch or voice_map lookup

                elif eff.effect_type == 'smpsNoteFill':
                    note_fill = eff.params[0]

                elif eff.effect_type == 'smpsModSet':
                    # wait, speed, change, steps
                    vibrato_wait   = eff.params[0]
                    _smps_speed_raw = eff.params[1]
                    vibrato_change = eff.params[2]   # raw delta; scaled to period units at placement
                    vibrato_steps  = eff.params[3]
                    vibrato_speed = self._vibrato_speed(_smps_speed_raw, vibrato_steps, chan_cfg.source,
                                                        event.tick_position)
                    vibrato_active = True

                elif eff.effect_type == 'smpsModOn':
                    vibrato_active = True

                elif eff.effect_type == 'smpsModOff':
                    vibrato_active = False

                # smpsSetvoice, smpsPan, smpsChangeTransposition, smpsPSGform and
                # smpsPSGvoice are st.apply's business; smpsNop has no MOD equivalent.
                continue

            if event.is_note:
                note = event.note
                tick = event.tick_position

                if not note.is_rest and not router.plays_here(event):
                    # This note plays on its group's primary channel in this pattern
                    # (merge_patterns), or nowhere (dropped).  What still rings here from a
                    # pattern the channel was live in ends now, as the re-key ended it on the
                    # hardware; the channel's cells stay empty otherwise.
                    if last_inst is None:
                        continue
                    pattern, row = self._tick_to_pattern_row(tick)
                    if pattern >= self.config.max_patterns:
                        continue
                    mod_chan = router.current(tick)
                    if self.mod.note_at(pattern, row, mod_chan):
                        last_inst = None
                        continue            # a note-on already takes the column here
                    rate = _release_rows(last_inst, tick)
                    if rate is not None and not router.borrowed(mod_chan, tick):   # a slide would sit on its notes
                        self._write_release(mod_chan, pattern * 64 + row, last_vol, rate, tick,
                                            _next_note_row(tick))
                    else:
                        self._set_cursor(pattern, mod_chan, row)
                        self.mod.set_effect(0xC, 0)
                    last_inst = None
                    continue

                if note.is_rest:
                    # is_no_attack=True marks an FM/DAC standalone-duration continuation —
                    # the YM2612 envelope sustains naturally; do not emit C00.
                    if note.is_no_attack:
                        if sounding_cents is not None and last_inst is not None and not st.is_psg:
                            _retune_tie(tick)
                        continue
                    pattern, row = self._tick_to_pattern_row(tick)
                    if pattern >= self.config.max_patterns:
                        continue
                    if last_inst is None and router.away(event):
                        continue            # nothing of this channel's sounds here: no C00 clutter
                    mod_chan = router.current(tick)
                    if (pattern, row) == (0, 0):
                        # A leading rest.  Its C00 matters once the song loops back to
                        # position 0, and the cell may hold a tempo command, so it is
                        # placed after every channel is converted (_place_leading_rests).
                        self._leading_rest_channels[mod_chan] = chan_cfg.source
                        continue
                    # Another channel's notes take this column here (mod_channel): its note-on
                    # ends this ring by itself, and a slide would sit on its notes
                    borrowed = router.borrowed(mod_chan, tick)
                    if borrowed and self.mod.note_at(pattern, row, mod_chan):
                        continue
                    rate = None if borrowed else _release_rows(last_inst, tick)
                    if rate is not None:
                        # Key-off: the note fades at the voice's release rate (the sample loops,
                        # or would be cut short of the chip's release either way)
                        self._write_release(mod_chan, pattern * 64 + row, last_vol, rate, tick,
                                            _next_note_row(tick))
                        continue
                    self._set_cursor(pattern, mod_chan, row)
                    self.mod.set_effect(0xC, 0)  # C00: mute channel
                    continue

                # Calculate pattern/row from tick
                pattern, row = self._tick_to_pattern_row(tick)

                if pattern >= self.config.max_patterns:
                    self._add_warning({
                        'type': 'pattern_overflow',
                        'channel': chan_cfg.source,
                        'pattern': pattern,
                        'max': self.config.max_patterns,
                    })
                    break

                self._set_cursor(pattern, mod_chan, row)
                note_delay = 0

                if is_dac and res is None:
                    # DAC notes carry no other effect, so the slot is always free for EDx.
                    pattern, row, note_delay = _note_cell(tick, True, None)
                    if pattern >= self.config.max_patterns:
                        break
                    mod_chan = router.take(pattern, row, tick, last_inst is not None)
                    self._set_cursor(pattern, mod_chan, row)
                    self._clear_stale_cut(pattern, row, mod_chan)
                    last_note_cell = (pattern, row)
                    # DAC: look up instrument and note from dac_samples config
                    dac_cfg = dac_map.get(note.dac_name)
                    if dac_cfg:
                        dac_inst, dac_note, region = router.drum(
                            tick, dac_cfg.mod_instrument, _MOD_NOTE_MAP.get(dac_cfg.mod_note, ModNote.C3))
                        self.mod.set_note(dac_note, dac_inst)
                        last_inst, last_vol = dac_inst, _sample_vol_map.get(dac_inst, 64)
                        bank_member = (self._merge.bank_members.get((chan_cfg.source, tick))
                                       if self._merge is not None else None)
                        if region is not None:
                            # A sound inside a sample bank (core.banks): start at its offset and
                            # cut the note once it is over, before the next sound in the slot
                            offset, sound = region
                            if offset:
                                self.mod.set_effect(0x9, offset >> 8)
                                if note_delay:
                                    note_delay = 0            # the slot holds the offset
                                    self._bank_delays_dropped += 1
                            rate = (self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock) \
                                / PERIOD_TABLE[dac_note.value]
                            self._bank_cuts += self._cut_after(mod_chan, tick, sound / rate, _next_note_row(tick))
                            self._set_cursor(pattern, mod_chan, row)
                    else:
                        # Fallback: use default instrument and C3
                        self.mod.set_note(ModNote.C3, st.instrument)
                        last_inst, last_vol = st.instrument, 64
                    if note_delay:
                        self.mod.set_effect(0xE, 0xD0 | note_delay)
                else:
                    # Melodic: which instrument and which MOD note (resolve_note, through
                    # walk_channel: the range lookup in the config's range_space, root +
                    # (key - low) for an anchored entry, the channel transpose otherwise).
                    assert res is not None
                    note_gain = res.gain_db
                    source_semitone = res.source
                    final_instrument = res.instrument
                    final_note = ModNote(router.note(tick, res.index))
                    active_range_entry = None if is_psg else res.entry
                    self._warn_resolution(res, st, chan_cfg, note)
                    # A solo note carries none of this channel's modulation, but its own note
                    # fill (the follower's smpsNoteFill, on the NoteOn core.merge spliced it
                    # from), and a PSG note ends at its duration wherever it plays (an FM one on
                    # a PSG channel does not: it rings into its rest's release)
                    _solo = getattr(event, "merged", None)
                    _vib_on = vibrato_active and res.path != "merged"
                    _nf = note_fill if _solo is None else _solo.fill
                    _psg_note = is_psg if _solo is None else _solo.kind == "PSG"

                    # Where the note goes (see _note_cell): on its own row with an EDx delay when
                    # it starts between rows and the effect slot is free.  The slot is needed
                    # for Cxx when this note's level differs from the instrument's, and for ECx
                    # when the note is cut inside the attack row (note fill; PSG notes also end
                    # at their duration).
                    _fill_t = _nf * self._tpf_at(tick)
                    _cut_tick = None
                    if _nf > 0 and _fill_t < note.duration:
                        _cut_tick = tick + _fill_t
                    elif _psg_note:
                        _cut_tick = tick + _ring_ticks.get(id(event), note.duration)
                    # A Cxx due on the attack row gives way to EDx when the note lasts into the
                    # next row: the volume is then set there (see cxx_coord below).  Drowning FM4
                    # pans every other note hard, so half its notes carry a -3 dB Cxx, and all of
                    # them start a tick off the grid.
                    # A sound inside a sample bank (core.banks): the note starts with 9xx at its
                    # offset, so the attack row's effect slot is the offset's
                    bank_member = (self._merge.bank_members.get((chan_cfg.source, tick))
                                   if self._merge is not None and _solo is None else None)
                    _region = (self._merge.region_at(chan_cfg.source, tick)
                               if bank_member is not None and self._merge is not None else None)
                    _bank9 = _region is not None and _region[0] > 0
                    _needs_cxx = _emit_volume(final_instrument) != _sample_vol_map.get(final_instrument, 64)
                    # smpsNoAttack before a note byte: the driver writes the new frequency and
                    # skips the key-on (a grace note bending into the chord, Drowning's slides).
                    # A MOD note re-triggers its sample, so the note is written with a tone
                    # portamento at full speed instead (3FF: the period slides in a tick, no
                    # re-trigger; the instrument number only resets the volume).  It needs the
                    # effect slot, so no EDx, and a Cxx due moves to the next row.
                    legato_mode = self.synth.legato if self.synth else "strict"
                    legato = note.is_no_attack and res.path != "merged" and legato_mode != "retrigger"
                    if _region is not None:
                        legato = False          # a banked sound starts at its offset: a note-on
                    strict = legato_mode == "strict"
                    if strict and legato and last_inst is None:
                        # Nothing has sounded on this channel yet: a portamento would never
                        # trigger a sample (Drowning's FM3 trill is no-attack from its first
                        # note; the hardware plays it).
                        legato = False
                    if strict and legato and last_voice is not None and st.voice != last_voice:
                        # smpsSetvoice between the notes: the hardware rewrites the operators
                        # under the running envelope, so the note sounds with the new voice.  A
                        # portamento would keep the old voice's sample; re-trigger on the new one
                        # (Green Hill's FM4/FM5 at the loop label: voice $08 -> $05).
                        legato = False
                    if strict and legato and last_inst is not None and last_chip is not None and last_inst != final_instrument:
                        # The target lies in another range of the voice (another instrument, its
                        # sample rendered for another octave).  A portamento never changes the
                        # sample, so the slide is written on the one that is sounding: the same
                        # chip pitch, as many semitones from the previous MOD note as it is from
                        # the previous chip pitch (Green Hill's FM3 grace C6 -> B5 crosses voice
                        # $08's C6 range boundary and landed an octave up).  Off the MOD's three
                        # octaves, the note is re-triggered on its own instrument instead.
                        shifted = last_idx + (res.chip - last_chip)
                        if 0 <= shifted <= 35:
                            final_instrument, final_note = last_inst, ModNote(shifted)
                        else:
                            legato = False
                    pattern, row, note_delay = _note_cell(
                        tick, not legato and not _bank9 and (not _needs_cxx or note.duration >= 2 * self._effective_tpr),
                        _cut_tick)
                    if pattern >= self.config.max_patterns:
                        break
                    mod_chan = router.take(pattern, row, tick, last_inst is not None)
                    self._set_cursor(pattern, mod_chan, row)
                    self._clear_stale_cut(pattern, row, mod_chan)
                    last_note_cell = (pattern, row)

                    self.mod.set_note(final_note, final_instrument)
                    last_inst, last_vol = final_instrument, _emit_volume(final_instrument)
                    last_idx, last_chip, last_voice = final_note.value, res.chip, st.voice
                    if self._detune is not None and not _psg_note:
                        sounding_cents = self._sample_cents(final_instrument)
                        sounding_period = PERIOD_TABLE[final_note.value]

                    # Note fill: silence the channel when the driver fires
                    # PSGNoteOff/FMNoteOff.  The fill byte counts V-int FRAMES (it is
                    # decremented on TempoWait frames too), so it is scaled onto the tick
                    # timeline first.  Skip when the fill outlasts the note: DurationTimeout
                    # expires first and the fill timer never completes (note sustains).
                    fill_placed = False
                    effect_slot_used = False   # True only when ECx occupies the current row's slot
                    fill_pat = fill_row = -1
                    slide_coords: set[tuple[int, int]] = set()   # rows a release slide took
                    fill_ticks = _nf * self._tpf_at(tick)
                    if _nf > 0 and fill_ticks < note.duration:
                        # Work in absolute MOD ticks (rows × speed) so the cut keeps its
                        # sub-row position: a whole row → C00 on that row, otherwise ECx.
                        speed = self.config.target_speed
                        row_abs = (pattern * 64 + row) * speed
                        note_abs = row_abs + note_delay
                        next_pat, next_row = self._tick_to_pattern_row(tick + note.duration)
                        next_abs = (next_pat * 64 + next_row) * speed
                        fill_abs = max(note_abs + 1,
                                       round((tick + fill_ticks) * speed / self._effective_tpr))
                        # Effect priority: volume beats note cut.  If the attack row needs its
                        # slot for Cxx, the cut moves to the start of the next row instead.
                        _sv = _sample_vol_map.get(final_instrument, 64)
                        if fill_abs < row_abs + speed and (_emit_volume(final_instrument) != _sv or _bank9):
                            fill_abs = row_abs + speed      # the attack row's slot holds Cxx / 9xx
                        fill_row_total, fill_sub = divmod(fill_abs, speed)
                        # At or past the row of the next event, the next note / rest takes over.
                        if fill_abs < next_abs and fill_row_total // 64 < self.config.max_patterns:
                            fill_pat, fill_row = fill_row_total // 64, fill_row_total % 64
                            rel_rate = _release_rows(final_instrument, tick)
                            if rel_rate is not None and not (
                                    (fill_pat, fill_row) == (pattern, row) and (note_delay or legato)):
                                # The fill is a key-off: the voice releases from that row on
                                # (the sub-row position is given up for the slide's slot)
                                slide_coords.update(self._write_release(
                                    mod_chan, fill_row_total, _emit_volume(final_instrument), rel_rate,
                                    tick, min(_next_note_row(tick), next_pat * 64 + next_row)))
                            else:
                                self._set_cursor(fill_pat, mod_chan, fill_row)
                                if fill_sub:
                                    self.mod.set_effect(0xE, 0xC0 | fill_sub)
                                else:
                                    self.mod.set_effect(0xC, 0)
                            # Restore cursor to the current note's cell.
                            self._set_cursor(pattern, mod_chan, row)
                            fill_placed = True
                            # ECx (or the slide's first A0y) on the attack row leaves no room
                            # for Cxx / 4xy there.
                            effect_slot_used = ((pattern, row) in slide_coords
                                                or ((fill_pat, fill_row) == (pattern, row) and rel_rate is None))

                    # PSG auto note-cut: emit silence at the note's natural end if no
                    # explicit smpsNoteFill was placed.  Mirrors hardware PSGDoNext
                    # setting vol=15 when the duration timer expires - after the
                    # smpsNoAttack continuations, which do not re-key (_ring_ticks).
                    if _psg_note and not fill_placed:
                        cut_tick = tick + _ring_ticks.get(id(event), note.duration)
                        cut_pat, cut_row = self._tick_to_pattern_row(cut_tick)
                        if cut_pat == pattern and cut_row == row and _bank9:
                            # The attack row's slot holds the 9xx: the cut waits for the next row
                            nxt = pattern * 64 + row + 1
                            if divmod(nxt, 64) not in _note_on_positions and nxt // 64 < self.config.max_patterns:
                                self.mod.ensure_pattern(nxt // 64)
                                self._set_cursor(nxt // 64, mod_chan, nxt % 64)
                                self.mod.set_effect(0xC, 0)
                                self._set_cursor(pattern, mod_chan, row)
                        elif cut_pat == pattern and cut_row == row:
                            # Sub-row cut: note ends within the same MOD row → ECx
                            ec_val = round(
                                (cut_tick - tick) * self.config.target_speed
                                / self._effective_tpr
                            )
                            ec_val = min(ec_val, self.config.target_speed - 1)
                            if ec_val > 0:
                                self.mod.set_effect(0xE, 0xC0 | ec_val)
                        elif (cut_pat, cut_row) not in _note_on_positions \
                                and cut_pat < self.config.max_patterns:
                            # Different row: write C00 only where no note-on fires
                            # (rest events also emit C00 there, which is idempotent)
                            self._set_cursor(cut_pat, mod_chan, cut_row)
                            self.mod.set_effect(0xC, 0)
                            self._set_cursor(pattern, mod_chan, row)

                    # A delayed note spends its slot on EDx (an in-row ECx was ruled out by
                    # _note_cell; an attack-row 4xy is given up — the later rows carry it).
                    cxx_coord = None
                    if note_delay:
                        if effect_slot_used:          # cannot happen; keep the cut if it does
                            note_delay = 0
                        else:
                            self.mod.set_effect(0xE, 0xD0 | note_delay)
                            effect_slot_used = True
                    if legato and not effect_slot_used:
                        self.mod.set_effect(0x3, 0xFF)
                        effect_slot_used = True
                    if _bank9:
                        self.mod.set_effect(0x9, _region[0] >> 8)   # type: ignore[index]
                        effect_slot_used = True
                        self._bank_cxx_moved += _needs_cxx
                    if (note_delay or legato or _bank9) and _needs_cxx:
                        # The Cxx moves to the first later row of the note whose slot is free
                        # (a cut placed above keeps its row).  One row at the instrument's own
                        # level, then the right one; a lost row of level beats 33 ms of timing.
                        end_pat, end_row = self._tick_to_pattern_row(tick + note.duration)
                        r_total = pattern * 64 + row + 1
                        while r_total < end_pat * 64 + end_row and r_total // 64 < self.config.max_patterns:
                            p_, r_ = divmod(r_total, 64)
                            self.mod.ensure_pattern(p_)
                            if self.mod.effect_slot_free(p_, r_, mod_chan):
                                self._set_cursor(p_, mod_chan, r_)
                                self.mod.set_effect(0xC, _emit_volume(final_instrument))
                                self._set_cursor(pattern, mod_chan, row)
                                cxx_coord = (p_, r_)
                                break
                            r_total += 1

                    # Determine effective vibrato: per-entry override takes priority.
                    _vib_override = None
                    if active_range_entry is not None and active_range_entry.vibrato is not None:
                        _vib_override = active_range_entry.vibrato
                    elif st.psg_entry is not None and st.psg_entry.vibrato is not None:
                        _vib_override = st.psg_entry.vibrato

                    if _vib_override is not None:
                        eff_vib_speed = (_vib_override >> 4) & 0xF
                        eff_vib_depth = _vib_override & 0xF
                    else:
                        eff_vib_speed = vibrato_speed
                        # Depth is per note: the driver's swing is a fixed number of FNUM / divider
                        # units, so its size in cents depends on the chip note it is added to.
                        eff_vib_depth = self._vibrato_depth(
                            vibrato_change, vibrato_steps, PERIOD_TABLE[final_note.value],
                            source_semitone + st.transpose, is_psg, self._player)
                        if eff_vib_depth == 0:
                            eff_vib_speed = 0

                    if not effect_slot_used:
                        # Emit Cxx only when the scaled output differs from the
                        # instrument's own sample volume — MOD auto-resets to
                        # sample volume on each note trigger, so no command is
                        # needed when the volume is at its default.
                        sv = _sample_vol_map.get(final_instrument, 64)
                        emit_vol = _emit_volume(final_instrument)
                        if emit_vol != sv:
                            self.mod.set_effect(0xC, emit_vol)

                        # Vibrato effect (4xy) on attack row — only when the modulation
                        # wait is over for most of it (see vib_start_tick below).
                        elif (_vib_on and eff_vib_speed > 0
                              and vibrato_wait * self._tpf_at(tick) <= self._effective_tpr / 2):
                            param = (eff_vib_speed << 4) | eff_vib_depth
                            self.mod.set_effect(0x4, param)

                    # Emit 4xy on every continuation row within the note's vibrato span.
                    # In ProTracker, 4xy only applies on rows where the effect is present,
                    # so we repeat it each row to get continuous vibrato matching SMPS
                    # modulation.  The SMPS wait is in FRAMES (DoModulation runs on TempoWait
                    # frames too); a row carries 4xy when modulation runs for at least half of it.
                    if _vib_on and eff_vib_speed > 0:
                        vib_start_tick = tick + vibrato_wait * self._tpf_at(tick)
                        note_end_tick  = tick + note.duration
                        tpr = self._effective_tpr
                        fill_coord = (fill_pat, fill_row) if fill_placed else None
                        cont_tick = tick + tpr   # start one row past the attack
                        while cont_tick < note_end_tick:
                            if cont_tick + tpr / 2 >= vib_start_tick:
                                cont_pat, cont_row = self._tick_to_pattern_row(cont_tick)
                                if cont_pat >= self.config.max_patterns:
                                    break
                                if slide_coords and (cont_pat, cont_row) >= min(slide_coords):
                                    break                   # released: nothing to modulate
                                if (cont_pat, cont_row) not in (fill_coord, cxx_coord):
                                    if cont_pat >= len(self.mod.patterns):
                                        break
                                    self._set_cursor(cont_pat, mod_chan, cont_row)
                                    vib_param = (eff_vib_speed << 4) | eff_vib_depth
                                    self.mod.set_effect(0x4, vib_param)
                            cont_tick += tpr
                        # Restore cursor to the attack row
                        self._set_cursor(pattern, mod_chan, row)

                    # A banked sound would run on into the next one in its bank: cut once it is
                    # over, unless the channel's next note comes first (a looped sound is its
                    # bank's last and needs none)
                    if _region is not None and bank_member is not None and not bank_member.looped:
                        clock = self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock
                        self._bank_cuts += self._cut_after(mod_chan, tick,
                                                           _region[1] / (clock / PERIOD_TABLE[final_note.value]),
                                                           _next_note_row(tick))
                        self._set_cursor(pattern, mod_chan, row)

    _MAX_RELEASE_ROWS = 64

    def _write_release(self, mod_chan: int, row_total: int, volume: int, rate_db_s: float,
                       tick: int, stop_row_total: int) -> set[tuple[int, int]]:
        """End a note the way the chip does: a volume slide from `volume` at the voice's release
        rate, one `A0y` per row from `row_total` on, stopping before `stop_row_total` (the next
        note-on's row) or once the volume is gone.

        The YM2612 release is linear in dB, so the target volume falls by the same ratio every
        row; each row's y is what takes the volume from where the last row left it to where
        the curve is at the row's end (rows whose share rounds to nothing are skipped, so a slow
        release keeps its pace).  A row whose effect slot is taken is skipped.  If the volume
        is still up after _MAX_RELEASE_ROWS (a release rate of 0, which rings on the hardware),
        a C00 ends it.  Returns the (pattern, row) cells written.
        """
        speed = self.config.target_speed
        row_secs = speed * 2.5 / self._bpm_for(self._segment_at(tick)[1])
        per_tick = speed - 1
        written: set[tuple[int, int]] = set()
        v = float(volume)
        target = float(volume)
        r = row_total
        while v > 0 and r < stop_row_total and r // 64 < self.config.max_patterns:
            if r - row_total >= self._MAX_RELEASE_ROWS:
                self._set_cursor(r // 64, mod_chan, r % 64)
                if self.mod.effect_slot_free(r // 64, r % 64, mod_chan):
                    self.mod.set_effect(0xC, 0)
                    written.add((r // 64, r % 64))
                break
            target *= 10 ** (-rate_db_s * row_secs / 20.0)
            y = round((v - target) / per_tick)
            if y > 0:
                y = min(15, y)
                self.mod.ensure_pattern(r // 64)
                if self.mod.effect_slot_free(r // 64, r % 64, mod_chan):
                    self._set_cursor(r // 64, mod_chan, r % 64)
                    self.mod.set_effect(0xA, y)
                    written.add((r // 64, r % 64))
                    v = max(0.0, v - y * per_tick)
            r += 1
        return written

    def _clear_stale_cut(self, pattern: int, row: int, mod_chan: int) -> None:
        """Drop a C00 left in this cell by an earlier rest whose row rounds onto this note-on's:
        set_note keeps the effect bytes, and a note-on with C00 is a silent note."""
        if not self.mod.note_at(pattern, row, mod_chan) and self.mod.effect_at(pattern, row, mod_chan) == (0xC, 0):
            self._set_cursor(pattern, mod_chan, row)
            self.mod.set_effect(0, 0)

    def _warn_resolution(self, res: ResolvedNote, st: DriverState, chan_cfg: ChannelConfig, note) -> None:
        """Warn where a note was clamped to the MOD's three octaves, or fell outside every range
        of a mapped voice (a config gap rather than an intended fallback)."""
        source = chan_cfg.source
        src_name = _semitone_to_name(res.source)
        if res.path == "transpose" and st.fm_ranges(source) and st.voice is not None:
            ranges = st.fm_ranges(source)
            self._add_warning({
                'type': 'map_gap', 'channel': source, 'voice_idx': st.voice,
                'extra_ctx': st.psg_label, 'note_name': src_name, 'semitone': res.source,
                'range_lo': _semitone_to_name(ranges[0].low),
                'range_hi': _semitone_to_name(ranges[-1].high),
            })
        if not res.clamped:
            return
        high = res.raw_index > 35
        entry = res.entry
        w = {'type': 'clamp_high' if high else 'clamp_low', 'channel': source,
             'src_name': src_name, 'note_value': note.note_value, 'transpose': 0}
        if res.path == "fm_root":
            assert entry is not None
            w.update(voice_idx=st.voice,
                     boundary=_semitone_to_name(entry.high if high else entry.low))
        elif res.path == "psg_root":
            assert entry is not None
            # The source note at which the anchor runs off the MOD's range
            edge = entry.low + ((35 if high else 0) - entry.root.value)
            w.update(voice_idx=None, extra_ctx=st.psg_label, boundary=_semitone_to_name(edge))
        else:
            tr = res.total_transpose
            w.update(voice_idx=st.voice, extra_ctx=st.psg_label, transpose=tr,
                     boundary=_semitone_to_name((35 if high else 0) - tr))
            if source.startswith('PSG') and not st.psg_label and self.config.psg_voice_map:
                w['psg_available_labels'] = list(self.config.psg_voice_map.keys())
        self._add_warning(w)

    def _loop_target_tick(self) -> int | None:
        """Tick the song loops back to: the latest smpsJump target over the channels (the
        Bxx target, _set_loop_point); None when no channel jumps."""
        label_tick_pos = self.song.label_tick_pos
        target = None
        for ch in self.song.channels:
            if ch.has_jump and ch.jump_target_label:
                tick = label_tick_pos.get(ch.jump_target_label)
                if tick is not None and (target is None or tick > target):
                    target = tick
        return target

    def _place_leading_rests(self) -> None:
        """C00 at pattern 0 row 0 for every channel whose first event is a rest.

        Nothing plays there on the first pass, but a song that loops to position 0 (Robotnik,
        Special Stage) comes back with the last note before the Bxx still ringing, and this
        C00 is what ends it: without one the note rang through the leading rest until the
        channel's next event.  The Fxx speed and BPM commands are placed after this
        (_place_tempo_commands) in the cells left free.  A note delayed into row 0 (EDx)
        restarts the sample itself.  When no cell is free the C00 is dropped, with a warning
        if the loop does return to row 0 (Star Light rests on all nine channels but loops to
        position 1: no warning).
        """
        if not self._leading_rest_channels:
            return
        loops_to_row0 = self._loop_target_tick() == 0
        used = {c.mod_channel for c in self.config.channels if c.enabled}
        order = ([c for c in range(self.mod.CHANNELS) if c not in used]
                 + [c for c in sorted(used) if c not in self._leading_rest_channels])
        for ch in sorted(self._leading_rest_channels):
            if self.mod.note_at(0, 0, ch):
                continue
            eff, par = self.mod.effect_at(0, 0, ch)
            if (eff, par) != (0, 0):
                slot = self.mod.free_effect_channel(0, 0, order)
                if slot is None:
                    if loops_to_row0:
                        self._add_warning({'type': 'rest_no_slot',
                                           'channel': self._leading_rest_channels[ch], 'mod_channel': ch})
                    continue
                self._set_cursor(0, slot, 0)
                self.mod.set_effect(eff, par)
            self._set_cursor(0, ch, 0)
            self.mod.set_effect(0xC, 0)

    def _place_tempo_commands(self) -> None:
        """The BPM and speed (Fxx) on pattern 0 row 0, in cells whose effect slot is free.

        They used to be written before the channels, on channels 0 and 1, where a note's own
        effect on row 0 (a Cxx, a legato 3FF) silently overwrote them: Special Stage lost its
        speed 3 and played at half tempo.  Now they go last: a spare channel first, then any
        channel whose row-0 cell has no effect; failing that, a leading rest's C00 gives way
        (the tempo matters more than one ring through the first rest).
        """
        wanted = [(0xF, self.config.target_bpm)]
        if self.config.target_speed != 6:
            wanted.insert(0, (0xF, self.config.target_speed))
        used = {c.mod_channel for c in self.config.channels if c.enabled}
        order = [c for c in range(self.mod.CHANNELS) if c not in used] + sorted(used)
        for eff, par in wanted:
            slot = self.mod.free_effect_channel(0, 0, order)
            if slot is None:
                # Take a leading rest's C00 (a cell with no note); a sample restart (EDx)
                # or a note's own command stays.  Worth a warning only where the song loops
                # to row 0, as in _place_leading_rests: a song that ends (smpsStop, no jump)
                # has keyed every track off before the player wraps (the Title Screen).
                for ch in order:
                    if not self.mod.note_at(0, 0, ch) and self.mod.effect_at(0, 0, ch) == (0xC, 0):
                        slot = ch
                        if self._loop_target_tick() == 0:
                            self._add_warning({'type': 'rest_no_slot', 'mod_channel': ch,
                                               'channel': self._leading_rest_channels.get(ch, f'MOD channel {ch}')})
                        break
            if slot is None:
                self._add_warning({'type': 'tempo_no_slot', 'pattern': 0, 'row': 0,
                                   'modifier': self.song.header.tempo_modifier, 'bpm': par})
                continue
            self._set_cursor(0, slot, 0)
            self.mod.set_effect(eff, par)

    def _tick_to_pattern_row(self, tick):
        """Convert a tick position to (pattern_index, row_within_pattern).

        Args:
            tick: Cumulative tick position

        Returns:
            (pattern, row) tuple
        """
        tpr = self._effective_tpr
        row_total = round(tick / tpr)
        pattern = row_total // 64
        row = row_total % 64
        return pattern, row

    def _set_loop_point(self, breaks=None):
        """Set Bxx position jump for song looping based on smpsJump targets.

        Must be called after apply_pattern_breaks so that the Bxx is placed
        at the correct post-break location and the target maps correctly.

        breaks: list of (pattern_slot, break_row) tuples from mod_pattern_breaks.
                When provided, the loop target tick is mapped to its post-break
                position by applying each break's shift in sorted order.
        """
        loop_target_tick = self._loop_target_tick()
        if loop_target_tick is None:
            return  # No smpsJump found; nothing to do

        tpr = self._effective_tpr

        # Derive last row from song tick data (handles rest/sustain tails that
        # a period-scan could not see because they have no note trigger).
        song_end_tick = 0
        for ch in self.song.channels:
            for ev in ch.events:
                end = ev.tick_position + (ev.note.duration if ev.note else 0)
                if end > song_end_tick:
                    song_end_tick = end
        song_end_flat = _shift_for_breaks(max(round(song_end_tick / tpr), 1) - 1, breaks)
        last_pattern, last_row = divmod(song_end_flat, 64)

        # Target: map loop_target_tick to post-break (pattern, row)
        flat_row = _shift_for_breaks(round(loop_target_tick / tpr), breaks)
        target_pattern, target_row = divmod(flat_row, 64)

        # The Bxx takes a free effect slot on the last row (channel 0 held a note cut there -
        # Green Hill's EC1 - and the jump overwrote it), among the columns already in use so a
        # merged build is not widened for it.  A Dxx must sit to its right: ProTracker reads a
        # row's effects left to right and a Bxx after a Dxx resets the break row to 0.
        cols = range(max(1, self.mod.used_channels()))
        need = 2 if target_row != 0 else 1
        free = [c for c in cols if self.mod.effect_slot_free(last_pattern, last_row, c)]
        b_chan = free[0] if len(free) >= need else 0
        if len(free) < need:
            eff = self.mod.effect_at(last_pattern, last_row, 0)
            self._add_warning({'type': 'loop_no_slot', 'channel': 'all', 'pattern': last_pattern,
                               'row': last_row, 'overwrote': eff})
        self._set_cursor(last_pattern, b_chan, last_row)
        self.mod.set_position_jump(target_pattern)

        # A song that loops back into a different tempo segment needs its BPM set again there
        # (the Fxx cells written by _write_tempo_changes sit at the changes, not at the target).
        segs = getattr(self, '_tempo_segments', None) or []
        if len(segs) > 1 and self._segment_at(loop_target_tick)[1] != segs[-1][1]:
            target_mod = self._segment_at(loop_target_tick)[1]
            ch = self.mod.free_effect_channel(target_pattern, target_row)
            if ch is not None:
                self._set_cursor(target_pattern, ch, target_row)
                self.mod.set_effect(0xF, self._bpm_for(target_mod))
            else:
                self._add_warning({'type': 'tempo_no_slot', 'channel': 'all', 'tick': loop_target_tick,
                                   'pattern': target_pattern, 'row': target_row, 'modifier': target_mod,
                                   'bpm': self._bpm_for(target_mod), 'exact_bpm': float('nan')})

        # If the target lands mid-pattern, write a Dxx companion on a free channel
        if target_row != 0:
            ch = self.mod.free_effect_channel(last_pattern, last_row, range(b_chan + 1, self.mod.CHANNELS))
            if ch is not None:
                self.mod.set_channel(ch)
                self.mod.set_effect(0xD, row_to_bcd(target_row))

        self.infos.append({
            'type': 'loop_set',
            'pattern': last_pattern,
            'row': last_row,
            'target': target_pattern,
        })
