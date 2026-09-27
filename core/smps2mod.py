"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.
"""

import bisect
import dataclasses

from .config import (
    ChannelConfig,
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    rate3_synth_root_issues,
)
from .driver_state import (
    DriverState,
    ResolvedNote,
    enabled_channels,
    resolve_synth_roots,
    walk_channel,
)
from .driver_state import source_map as source_map_for
from .driver_tables import PSG_FREQUENCIES_EXTENDED, psg_tone2_divider
from .levels import (
    DEFAULT_FM_PAN_LAW_DB,
    fm_level_db,
    fm_tl_to_mod,
    modal_level,
    psg_att_to_mod,
)
from .mod import ModFile, ModSample, row_to_bcd
from .pcm import MAX_MOD_SAMPLE_BYTES, max_sustain_secs
from .smps_parser import SmpsChannel, SmpsSong
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

# Map MOD note name strings to ModNote enum values
# Supports both "#" (F#3) and "s" (Fs3) sharp notation, plus "b" for flats
_MOD_NOTE_MAP = {}
for _oct in range(1, 4):
    _notes = [
        (f"C{_oct}", f"C{_oct}"),
        (f"C#{_oct}", f"Cs{_oct}"), (f"Cs{_oct}", f"Cs{_oct}"), (f"Db{_oct}", f"Cs{_oct}"),
        (f"D{_oct}", f"D{_oct}"),
        (f"D#{_oct}", f"Ds{_oct}"), (f"Ds{_oct}", f"Ds{_oct}"), (f"Eb{_oct}", f"Ds{_oct}"),
        (f"E{_oct}", f"E{_oct}"),
        (f"F{_oct}", f"F{_oct}"),
        (f"F#{_oct}", f"Fs{_oct}"), (f"Fs{_oct}", f"Fs{_oct}"), (f"Gb{_oct}", f"Fs{_oct}"),
        (f"G{_oct}", f"G{_oct}"),
        (f"G#{_oct}", f"Gs{_oct}"), (f"Gs{_oct}", f"Gs{_oct}"), (f"Ab{_oct}", f"Gs{_oct}"),
        (f"A{_oct}", f"A{_oct}"),
        (f"A#{_oct}", f"As{_oct}"), (f"As{_oct}", f"As{_oct}"), (f"Bb{_oct}", f"As{_oct}"),
        (f"B{_oct}", f"B{_oct}"),
    ]
    for _key, _enum_name in _notes:
        _MOD_NOTE_MAP[_key] = ModNote[_enum_name]
del _oct, _notes, _key, _enum_name


def _shift_for_breaks(flat_row: int, breaks: list[tuple[int, int]] | None) -> int:
    """Move a pre-break flat row index to where apply_pattern_breaks put it.

    Each break at (pattern P, row R) pushes everything from flat row P*64+R+1 onward
    to the start of pattern P+1, i.e. forward by the 63-R rows it blanked out.
    """
    for P, break_row in sorted(breaks or []):
        if flat_row >= P * 64 + break_row + 1:
            flat_row += 63 - break_row
    return flat_row


class SmpsToModConverter:
    def __init__(self, song: SmpsSong, config: ConversionConfig,
                 synth: SynthesisSettings | None = None,
                 psg_synth: PsgSynthesisSettings | None = None):
        self.song = song
        self.config = config
        self.synth = synth
        self.psg_synth = psg_synth
        self.mod = ModFile(channels=config.mod_channel_count)
        # Structured warnings and informational messages collected during conversion.
        # Public: convert.py renders both after convert() returns.
        self.warnings: list[dict] = []
        self.infos: list[dict] = []
        self._seen_warnings: set = set()
        self._leading_rest_channels: dict[int, str] = {}   # MOD channel -> source, see _place_leading_rests
        self._vib_rate_limited: set = set()

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
    def _vibrato_depth(delta: int, steps: int, period: int, chip_index: int, is_psg: bool) -> int:
        """ProTracker 4xy depth nibble for one note; 0 = too shallow to play.

        Driver: the accumulator swings delta * steps / 2 either side of its centre (first
        half-swing steps/2 steps, every later one the full `steps`), and is added to the note's
        own frequency word: the YM2612 FNUM of its pitch class (644 for C ... 1216 for B, the
        block is untouched) or the SN76489 divider of its PSGFrequencies entry.  ProTracker's
        sine peaks at 2 * y period units.  An Amiga period and a PSG divider are both 1/f and a
        small FNUM change is proportional to f, so in every case

            y = period * (delta * steps / 2) / frequency_word / 2

        Below 0.35 the smallest depth would overshoot the hardware by 3x or more: no vibrato.
        """
        word = (PSG_FREQUENCIES_EXTENDED[chip_index & 0x7F] if is_psg
                else _S1_FNUM_BASE * 2 ** ((chip_index % 12) / 12))
        if word <= 0:
            return 0
        if delta >= 0x80:
            delta -= 0x100
        exact = period * (abs(delta) * steps / 2) / word / 2
        return 0 if exact < 0.35 else max(1, min(0xF, round(exact)))

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
        sl_name_map = {e[0]: e[1] for e in sample_list} if sample_list else {}
        for inst_num, (pcm_orig, _) in samples_dict.items():
            pcm_data = pcm_orig[:max_bytes]
            if len(pcm_orig) > max_bytes:
                # The generators cap the sustain to the limit; this is a last resort.
                self._add_warning({'type': 'sample_truncated', 'channel': prefix,
                                   'extra_ctx': f'instrument {inst_num}', 'instrument': inst_num,
                                   'bytes': len(pcm_orig), 'max_bytes': max_bytes})
            sample = ModSample(sl_name_map.get(inst_num, f"{prefix}_inst{inst_num}"))
            sample.data = pcm_data
            sample.length = len(pcm_data) // 2
            sample.set_volume(64)
            self.mod.samples[inst_num - 1] = sample
        if sample_list:
            for entry in sample_list:
                inst_num_sl = entry[0]
                if inst_num_sl in samples_dict:
                    vol_sl = entry[2] if len(entry) > 2 else 64
                    ft_sl  = entry[3] if len(entry) > 3 else 0
                    self.mod.samples[inst_num_sl - 1].set_volume(vol_sl)
                    if ft_sl != 0:
                        self.mod.samples[inst_num_sl - 1].set_finetune(ft_sl)

    def _synthesis_roots(self, kind: str) -> dict[int, tuple[int, int]]:
        """{MOD instrument: (MOD note index its sample is synthesised for, synth_shift)}.

        The sample's own rate is `root`'s playback rate times 2^(synth_shift / 12) (the
        generators render synth_shift semitones above the pitch `root` sounds).

        The first entry that names an instrument decides, in the order the sample generators
        walk the maps; a later entry sharing the instrument anchors another range onto that
        same sample (Credits folds its voices into 31 slots this way, Stage Clear's PSG2 sits
        two octaves up its PSG1 sample).  FM: voice_map then channel_instrument_map, voices
        the song lacks skipped, a rootless channel_instrument_map entry at C1.  PSG: psg_map
        then psg_voice_map.  An instrument absent here is not synthesised (loaded from disk).
        """
        roots: dict[int, tuple[int, int]] = {}
        if kind == "FM":
            voices = {v.index for v in self.song.voices}
            cim = [(vi, e) for vim in self.config.channel_instrument_map.values()
                   for vi, rs in vim.items() for e in rs]
            rooted = [(vi, e) for vi, rs in self.config.voice_map.items() for e in rs] + cim
            for vi, e in rooted:
                if vi in voices and e.root is not None:
                    roots.setdefault(e.mod_instrument, (e.root.value, e.synth_shift))
            for vi, e in cim:
                if vi in voices and e.root is None:
                    roots.setdefault(e.mod_instrument, (ModNote.C1.value, 0))
        else:
            entries = list(self.config.psg_map.values())
            entries += [e for es in self.config.psg_voice_map.values() for e in es]
            for e in entries:
                if e.root is not None:
                    roots.setdefault(e.mod_instrument, (e.root.value, e.synth_shift))
        return roots

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
        needs: dict[int, tuple[float, tuple[int, int] | None]] = {}
        for chan_cfg, channel in enabled_channels(self.song, self.config, (kind,)):
            rings: list = []        # [start tick, ring ticks, instrument, out idx] or None
            for event, _st, res in walk_channel(channel, self.config, chan_cfg):
                if not event.is_note:
                    continue
                note = event.note
                if note.is_rest:
                    if note.is_no_attack and rings and rings[-1] is not None:
                        rings[-1][1] += note.duration       # continuation: no C00, keeps advancing
                    else:
                        rings.append(None)                  # C00 ends the ring
                    continue
                if res is None:
                    continue
                rings.append([event.tick_position, note.duration, res.instrument, res.index])

            for ring in rings:
                if ring is None:
                    continue
                start, ticks, inst, out_idx = ring
                secs = self._tick_span_secs(start, start + ticks + self._effective_tpr)
                root = roots.get(inst)
                if root is not None:
                    root_idx, shift = root
                    secs *= PERIOD_TABLE[root_idx] / PERIOD_TABLE[out_idx] / 2.0 ** (shift / 12.0)
                if finetunes.get(inst, 0) > 0:
                    secs *= 2.0 ** (finetunes[inst] / 96.0)
                prev = needs.get(inst)
                if prev is None or secs > prev[0]:
                    needs[inst] = (secs, root)
        return needs

    _AUTO_SUSTAIN_CAP_SECS = 10.0

    def _resolve_sustain(self, settings, kind: str):
        """Settings with `sustain_duration: auto` resolved to the longest ring any of the
        kind's instruments plays (capped at 10 s), and a warning for every synthesised
        instrument whose sample cannot hold one of its notes: the setting is shorter, the cap
        is, or the MOD sample limit at the instrument's rate is (max_sustain_secs)."""
        needs = self._sustain_needs(kind)
        auto = settings.sustain_duration == "auto"
        if auto:
            secs = min(max((n for n, _ in needs.values()), default=0.0), self._AUTO_SUSTAIN_CAP_SECS)
            if secs <= 0:
                secs = float(type(settings)().sustain_duration)    # no notes: the field's default
            settings = dataclasses.replace(settings, sustain_duration=secs)
            self.infos.append({'type': f'auto_sustain_{kind.lower()}', 'secs': round(secs, 3)})
        if not settings.enabled:
            return settings
        sustain = float(settings.sustain_duration)
        for inst, (need, root) in sorted(needs.items()):
            if root is None:
                continue
            root_idx, shift = root
            rate = round(settings.amiga_clock / PERIOD_TABLE[root_idx] * 2.0 ** (shift / 12.0))
            fits = max_sustain_secs(rate, settings.release_padding, settings.max_sample_bytes)
            have = min(sustain, fits)
            if need <= have + 0.005:
                continue
            limit = ('mod' if fits < sustain
                     else 'cap' if auto and need > self._AUTO_SUSTAIN_CAP_SECS
                     else 'setting')
            self._add_warning({'type': 'sustain_short', 'channel': kind, 'extra_ctx': f'instrument {inst}',
                               'kind': kind, 'instrument': inst, 'need': need, 'have': have,
                               'rate': rate, 'limit': limit, 'max_kb': settings.max_sample_kb})
        return settings

    def convert(self):
        """Main entry point. Returns a ModFile."""
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

        # Resolve 'auto' sustain durations from the longest ring each instrument plays, in the
        # MOD's own time, and warn where a sample cannot hold a note.
        synth = self._resolve_sustain(self.synth, 'FM') if self.synth else None
        psg_synth = self._resolve_sustain(self.psg_synth, 'PSG') if self.psg_synth else None

        # Load or synthesize samples
        if synth and synth.enabled and synth.mode == "ym2612":
            from ym2612.sample_generator import generate_fm_samples
            # Warn about voice_map entries whose voice index doesn't exist in the song,
            # and collect their instruments to suppress spurious "file not found" warnings.
            _voice_indices = {v.index for v in self.song.voices}
            fm_skipped_insts: set = set()
            for _vi, _ranges in self.config.voice_map.items():
                if _vi not in _voice_indices:
                    _insts = [e.mod_instrument for e in _ranges]
                    fm_skipped_insts.update(_insts)
                    print(f"Warning: voice_map[{_vi}] voice ${_vi:02X} not defined in song "
                          f"(inst {_insts}) — remove this entry from voice_map")
            # Each sample is rendered at the level most of its notes play at — the carriers carry
            # the channel volume as the driver's SetVoice writes it — so the chip clips a
            # multi-carrier voice as much as the hardware does at that level and no more.
            self._fm_render_levels = (self._plan_fm_render_levels()
                                      if self._fm_volume_mode == "baked" else {})
            fm_samples = generate_fm_samples(
                self.song, self.config, synth,
                tl_offsets={inst: lv[0] for inst, lv in self._fm_render_levels.items()})
            self.infos.append({'type': 'fm_synthesized', 'count': len(fm_samples)})
            self._install_synthesized_samples(fm_samples, self.config.sample_list, "fm",
                                              synth.max_sample_bytes)
            # Load remaining (DAC) samples from disk — skip FM-synthesized and PSG-synthesized instruments
            if self.config.sample_list:
                for entry in self.config.sample_list:
                    inst_num = entry[0]
                    if inst_num not in fm_samples and inst_num not in psg_synth_insts and inst_num not in fm_skipped_insts:
                        self.mod.add_samples(self.config.samples_dir, [entry])
        elif self.config.sample_list:
            # Load all disk samples, skipping any that will be PSG-synthesized
            for entry in self.config.sample_list:
                if entry[0] not in psg_synth_insts:
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

        # PSG synthesis block
        if psg_synth and psg_synth.enabled and (self.config.psg_map or self.config.psg_voice_map):
            from sn76489.sample_generator import generate_psg_samples
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
            psg_samples = generate_psg_samples(
                self.config, psg_synth, rate3_dividers={i: d['n'] for i, d in rate3.items()},
                noise_envelopes={i: d['envelope'] for i, d in noise_env.items()})
            self._install_synthesized_samples(psg_samples, self.config.sample_list, "psg",
                                              psg_synth.max_sample_bytes)
            self.infos.append({'type': 'psg_synthesized', 'count': len(psg_samples)})

        # Set timing
        self.mod.set_bpm(self.config.target_bpm)
        if self.config.target_speed != 6:
            self.mod.set_speed(self.config.target_speed)

        # Extend channels whose loop body is too short to cover the full song
        self._extend_looping_channels()
        self._tempo_segments = self._collect_tempo_segments()

        # Convert channels
        self._convert_all_channels()
        self._place_leading_rests()
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

    def _count_levels(self, kind: str, level_of) -> dict[int, dict]:
        """{MOD instrument: {level_of(state): notes}} over every enabled channel of `kind` ("FM" or
        "PSG"), walked with the same DriverState the conversion uses.  A PSG note at
        attenuation 15 is silent and does not vote.
        """
        counts: dict[int, dict] = {}
        for chan_cfg, channel in enabled_channels(self.song, self.config, (kind,)):
            for _event, st, res in walk_channel(channel, self.config, chan_cfg):
                if res is None or (st.is_psg and st.is_silent):
                    continue
                per = counts.setdefault(res.instrument, {})
                k = level_of(st)
                per[k] = per.get(k, 0) + 1
        return counts

    def _plan_levels(self, kind: str) -> dict[int, float]:
        """"baked" volume mode: the level (dB) each MOD instrument's sample_list volume stands for.

        The level with the most notes is the instrument's baseline — those notes need no Cxx.
        Ties go to the louder level so the others are attenuated rather than boosted past 64.

        FM levels come from the TL offset (smpsHeaderFM volume + smpsAlterVol) and the pan;
        PSG levels from the attenuation (smpsHeaderPSG volume + smpsPSGAlterVol).
        """
        pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
        counts = self._count_levels(kind, lambda st: st.level_db(pan_law))
        return {inst: modal_level(levels) for inst, levels in counts.items()}

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
        counts = self._count_levels("FM", lambda st: (st.tl, st.hard_panned))
        return {inst: max(per, key=lambda k: (per[k], fm_level_db(k[0], k[1], pan_law)))
                for inst, per in counts.items()}

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
                if base is not None and abs(fm_level_db(tl, pan, pan_law) - base) > 1e-9:
                    print(f"Warning: instrument {inst} was rendered at TL +{tl}"
                          f"{' panned' if pan else ''} ({fm_level_db(tl, pan, pan_law):+.2f} dB) but its "
                          f"baked level is {base:+.2f} dB — the loop extension changed the modal level")
        self._psg_baseline_db: dict[int, float] = {}
        if self._psg_volume_mode == "baked":
            self._psg_baseline_db = self._plan_levels("PSG")

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

    def _convert_channel(self, channel: SmpsChannel, chan_cfg: ChannelConfig, is_dac: bool):
        """Convert a single SMPS channel to MOD data."""
        mod_chan = chan_cfg.mod_channel

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
        _fm_baked = _fm_mode == "baked" and not is_psg and not is_dac
        _psg_baked = is_psg and self._psg_volume_mode == "baked"
        _pan_law = self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB
        if is_dac or (not is_psg and _fm_mode == "off"):
            st.tl = 0          # neither mode reads the smpsHeaderFM volume byte
        if is_psg:
            current_volume = round(psg_att_to_mod(st.att) * chan_cfg.volume / 64)
        elif _fm_vol_scaling and not is_dac:
            current_volume = round(fm_tl_to_mod(st.tl) * chan_cfg.volume / 64)

        def _emit_volume(inst: int) -> int:
            """MOD volume for a note on `inst` right now (equals the sample volume → no Cxx)."""
            sv = _sample_vol_map.get(inst, 64)
            if not (_psg_baked or _fm_baked):
                return round(current_volume * sv / 64)
            if _psg_baked and st.is_silent:
                return 0
            level = st.level_db(_pan_law)
            baseline = self._psg_baseline_db if _psg_baked else self._fm_baseline_db
            rel_db = level - baseline.get(inst, level)
            return max(0, min(64, round(sv * 10 ** (rel_db / 20.0) * chan_cfg.volume / 64)))

        _note_on_positions: set[tuple[int, int]] = set()
        if is_psg:
            for _ev in channel.events:
                if _ev.is_note and not _ev.note.is_rest:
                    _note_on_positions.add(self._tick_to_pattern_row(_ev.tick_position))
                    _note_on_positions.add(divmod(int(_ev.tick_position // self._effective_tpr), 64))

        last_note_cell: tuple[int, int] | None = None   # where this channel's previous note-on went

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

        for event, _st, res in walk_channel(channel, self.config, chan_cfg, st):
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

                if note.is_rest:
                    # is_no_attack=True marks an FM/DAC standalone-duration continuation —
                    # the YM2612 envelope sustains naturally; do not emit C00.
                    if note.is_no_attack:
                        continue
                    pattern, row = self._tick_to_pattern_row(tick)
                    if pattern >= self.config.max_patterns:
                        continue
                    if (pattern, row) == (0, 0):
                        # A leading rest.  Its C00 matters once the song loops back to
                        # position 0, and the cell may hold a tempo command, so it is
                        # placed after every channel is converted (_place_leading_rests).
                        self._leading_rest_channels[mod_chan] = chan_cfg.source
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

                if is_dac:
                    # DAC notes carry no other effect, so the slot is always free for EDx.
                    pattern, row, note_delay = _note_cell(tick, True, None)
                    if pattern >= self.config.max_patterns:
                        break
                    self._set_cursor(pattern, mod_chan, row)
                    last_note_cell = (pattern, row)
                    # DAC: look up instrument and note from dac_samples config
                    dac_cfg = dac_map.get(note.dac_name)
                    if dac_cfg:
                        dac_inst = dac_cfg.mod_instrument
                        dac_note = _MOD_NOTE_MAP.get(dac_cfg.mod_note, ModNote.C3)
                        self.mod.set_note(dac_note, dac_inst)
                    else:
                        # Fallback: use default instrument and C3
                        self.mod.set_note(ModNote.C3, st.instrument)
                    if note_delay:
                        self.mod.set_effect(0xE, 0xD0 | note_delay)
                else:
                    # Melodic: which instrument and which MOD note (resolve_note, through
                    # walk_channel: the range lookup in the config's range_space, root +
                    # (key - low) for an anchored entry, the channel transpose otherwise).
                    assert res is not None
                    source_semitone = res.source
                    final_instrument = res.instrument
                    final_note = ModNote(res.index)
                    active_range_entry = None if is_psg else res.entry
                    self._warn_resolution(res, st, chan_cfg, note)

                    # Where the note goes (see _note_cell): on its own row with an EDx delay when
                    # it starts between rows and the effect slot is free.  The slot is needed
                    # for Cxx when this note's level differs from the instrument's, and for ECx
                    # when the note is cut inside the attack row (note fill; PSG notes also end
                    # at their duration).
                    _fill_t = note_fill * self._tpf_at(tick)
                    _cut_tick = None
                    if note_fill > 0 and _fill_t < note.duration:
                        _cut_tick = tick + _fill_t
                    elif is_psg:
                        _cut_tick = tick + note.duration
                    # A Cxx due on the attack row gives way to EDx when the note lasts into the
                    # next row: the volume is then set there (see cxx_coord below).  Drowning FM4
                    # pans every other note hard, so half its notes carry a -3 dB Cxx, and all of
                    # them start a tick off the grid.
                    _needs_cxx = _emit_volume(final_instrument) != _sample_vol_map.get(final_instrument, 64)
                    pattern, row, note_delay = _note_cell(
                        tick, not _needs_cxx or note.duration >= 2 * self._effective_tpr, _cut_tick)
                    if pattern >= self.config.max_patterns:
                        break
                    self._set_cursor(pattern, mod_chan, row)
                    last_note_cell = (pattern, row)

                    self.mod.set_note(final_note, final_instrument)

                    # Note fill: silence the channel when the driver fires
                    # PSGNoteOff/FMNoteOff.  The fill byte counts V-int FRAMES (it is
                    # decremented on TempoWait frames too), so it is scaled onto the tick
                    # timeline first.  Skip when the fill outlasts the note: DurationTimeout
                    # expires first and the fill timer never completes (note sustains).
                    fill_placed = False
                    effect_slot_used = False   # True only when ECx occupies the current row's slot
                    fill_pat = fill_row = -1
                    fill_ticks = note_fill * self._tpf_at(tick)
                    if note_fill > 0 and fill_ticks < note.duration:
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
                        if fill_abs < row_abs + speed and _emit_volume(final_instrument) != _sv:
                            fill_abs = row_abs + speed
                        fill_row_total, fill_sub = divmod(fill_abs, speed)
                        # At or past the row of the next event, the next note / rest takes over.
                        if fill_abs < next_abs and fill_row_total // 64 < self.config.max_patterns:
                            fill_pat, fill_row = fill_row_total // 64, fill_row_total % 64
                            self._set_cursor(fill_pat, mod_chan, fill_row)
                            if fill_sub:
                                self.mod.set_effect(0xE, 0xC0 | fill_sub)
                            else:
                                self.mod.set_effect(0xC, 0)
                            # Restore cursor to the current note's cell.
                            self._set_cursor(pattern, mod_chan, row)
                            fill_placed = True
                            # ECx on the attack row leaves no room for Cxx / 4xy there.
                            effect_slot_used = (fill_pat, fill_row) == (pattern, row)

                    # PSG auto note-cut: emit silence at the note's natural end if no
                    # explicit smpsNoteFill was placed.  Mirrors hardware PSGDoNext
                    # setting vol=15 when the duration timer expires.
                    if is_psg and not fill_placed:
                        cut_tick = tick + note.duration
                        cut_pat, cut_row = self._tick_to_pattern_row(cut_tick)
                        if cut_pat == pattern and cut_row == row:
                            # Sub-row cut: note ends within the same MOD row → ECx
                            ec_val = round(
                                note.duration * self.config.target_speed
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
                    if note_delay and _needs_cxx:
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
                            source_semitone + st.transpose, is_psg)
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
                        elif (vibrato_active and eff_vib_speed > 0
                              and vibrato_wait * self._tpf_at(tick) <= self._effective_tpr / 2):
                            param = (eff_vib_speed << 4) | eff_vib_depth
                            self.mod.set_effect(0x4, param)

                    # Emit 4xy on every continuation row within the note's vibrato span.
                    # In ProTracker, 4xy only applies on rows where the effect is present,
                    # so we repeat it each row to get continuous vibrato matching SMPS
                    # modulation.  The SMPS wait is in FRAMES (DoModulation runs on TempoWait
                    # frames too); a row carries 4xy when modulation runs for at least half of it.
                    if vibrato_active and eff_vib_speed > 0:
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
                                if (cont_pat, cont_row) not in (fill_coord, cxx_coord):
                                    if cont_pat >= len(self.mod.patterns):
                                        break
                                    self._set_cursor(cont_pat, mod_chan, cont_row)
                                    vib_param = (eff_vib_speed << 4) | eff_vib_depth
                                    self.mod.set_effect(0x4, vib_param)
                            cont_tick += tpr
                        # Restore cursor to the attack row
                        self._set_cursor(pattern, mod_chan, row)

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
        channel's next event.  Row 0 also holds the Fxx speed and BPM commands (channels 0
        and 1, ModFile.set_bpm / set_speed); one in the way moves to a free cell on that row,
        spare channels first.  A note delayed into row 0 (EDx) restarts the sample itself.
        When no cell is free the C00 is dropped, with a warning if the loop does return to
        row 0 (Star Light rests on all nine channels but loops to position 1: no warning).
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

        self._set_cursor(last_pattern, 0, last_row)
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
            ch = self.mod.free_effect_channel(last_pattern, last_row, range(1, self.mod.CHANNELS))
            if ch is not None:
                self.mod.set_channel(ch)
                self.mod.set_effect(0xD, row_to_bcd(target_row))

        self.infos.append({
            'type': 'loop_set',
            'pattern': last_pattern,
            'row': last_row,
            'target': target_pattern,
        })
