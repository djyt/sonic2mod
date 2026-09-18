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
from .mod import ModFile, ModSample
from .smps_parser import SmpsChannel, SmpsSong
from .tables import (
    PERIOD_TABLE,
    ModNote,
    smps_note_to_mod_note,
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


def _psg_att_to_mod(att: int) -> int:
    """Convert SN76489 4-bit attenuation to MOD volume (0-64).

    SN76489 attenuation: 0=max, 15=silent, 2 dB per step.
    """
    if att >= 15:
        return 0
    return round(64 * 10 ** (-(att * 2) / 20.0))


def _fm_tl_to_mod(tl: int) -> int:
    """Convert YM2612 TL offset to MOD volume (0-64).

    TL offset: 0=max, 127=silent, 0.75 dB per step.
    Used for smpsHeaderFM initial_vol and smpsAlterVol deltas.
    """
    if tl >= 127:
        return 0
    return round(64 * 10 ** (-(tl * 0.75) / 20.0))


_TL_STEP_DB = 0.75        # YM2612 total level, dB per step
_PSG_STEP_DB = 2.0        # SN76489 attenuation, dB per step (15 = silent)


def _psg_range_entry(entries, source_semitone: int):
    """Entry of a multi-entry psg_voice_map list whose low/high bracket covers this note, or None."""
    if entries is None or len(entries) <= 1:
        return None
    for e in entries:
        lo = e.low if e.low is not None else 0        # 0 = C0 (semitone floor)
        hi = e.high if e.high is not None else 255    # 255 > B7 (~95), matches all
        if lo <= source_semitone <= hi:
            return e
    return None


def _pan_is_hard(params: list) -> bool:
    """True for smpsPan panLeft / panRight (params arrive as one 'panLeft, $00' string)."""
    direction = str(params[0]).split(',')[0].strip().lower() if params else ''
    return direction in ('panleft', 'panright')


class SmpsToModConverter:
    def __init__(self, song: SmpsSong, config: ConversionConfig,
                 synth: SynthesisSettings | None = None,
                 psg_synth: PsgSynthesisSettings | None = None):
        self.song = song
        self.config = config
        self.synth = synth
        self.psg_synth = psg_synth
        self.mod = ModFile(channels=config.num_mod_channels)
        # Structured warnings and informational messages collected during conversion.
        # Rendered by convert.py after convert() returns.
        self._warnings: list = []
        self._infos: list = []
        self._seen_warnings: set = set()
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
            self._warnings.append(w)

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

    @property
    def _ticks_per_frame(self) -> float:
        """Duration ticks that elapse per V-int frame: (modifier - 1) / modifier.

        TempoWait fires once every `modifier` frames and only does `addq.b #1` on each
        track's DurationTimeout, cancelling that frame's decrement.  NoteTimeoutUpdate
        (smpsNoteFill) and DoModulation (smpsModSet wait/speed) still run on those frames,
        so they count FRAMES while note durations and tick positions count TICKS.  Multiply a
        frame count by this to place it on the converter's tick timeline.

        Region-independent (both clocks scale with fps).  SFX have no tempo modifier.
        This is the HEADER value; mid-song smpsSetTempoMod is tracked in _tempo_segments -
        use _tpf_at(tick) wherever the tick is known.
        """
        mod = self.song.header.tempo_modifier
        if self.song.header.is_sfx or mod <= 1:
            return 1.0
        return (mod - 1) / mod

    def _vibrato_speed(self, mod_speed: int, steps: int, channel: str, tick=0) -> int:
        """ProTracker 4xy speed nibble for an smpsModSet (speed, steps) pair; 0 = cannot be played.

        Driver (DoModulation, once per V-int frame): every `speed` frames the delta is added; when
        the step counter runs out it is reloaded from the ORIGINAL steps byte, the delta is negated
        and one more step is spent.  Only the first half-swing uses the halved count, so the steady
        cycle is 2 * speed * (steps + 1) frames.  Not multiplied by the tempo divider.

        ProTracker: the vibrato position advances by x on each of a row's (speed - 1) processing
        ticks and wraps at 64.  One row is _effective_tpr driver ticks = _effective_tpr /
        _ticks_per_frame frames, so matching the two cycle lengths gives

            x = 64 * _effective_tpr / ((target_speed - 1) * cycle_frames * _ticks_per_frame)

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
                self._infos.append({'type': 'vibrato_rate_limit', 'channel': channel,
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
        if is_psg:
            from sfx.tables import PSG_FREQUENCIES_EXTENDED
            word = PSG_FREQUENCIES_EXTENDED[chip_index & 0x7F]
        else:
            word = _S1_FNUM_BASE * 2 ** ((chip_index % 12) / 12)
        if word <= 0:
            return 0
        if delta >= 0x80:
            delta -= 0x100
        exact = period * (abs(delta) * steps / 2) / word / 2
        return 0 if exact < 0.35 else max(1, min(0xF, round(exact)))

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
        """Duration ticks per V-int frame for a tempo modifier: (m - 1) / m (see _ticks_per_frame)."""
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
        stride = self.mod.CHANNELS * 4
        for start, modifier in self._tempo_segments[1:]:
            bpm = self._bpm_for(modifier)
            exact = self.config.target_bpm * self._tpf(modifier) / self._tpf(self.song.header.tempo_modifier)
            pattern, row = self._tick_to_pattern_row(start)
            if pattern >= self.config.max_patterns:
                break
            while pattern >= len(self.mod.patterns):
                self.mod.add_patterns(1)
            data = self.mod.patterns[pattern].get_bytes()
            slot = None
            for want_free in (True, False):
                for ch in order:
                    i = ch * 4 + row * stride
                    eff, par = data[i + 2] & 0x0F, data[i + 3]
                    free = eff == 0 and par == 0
                    vib_only = eff == 0x4 and not (data[i] & 0x0F or data[i + 1])
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
            self._infos.append(info)
            if not 32 <= exact <= 255:
                self._add_warning({'type': 'tempo_bpm_range', 'channel': 'all', **info})

    def _ticks_to_secs(self, ticks: int) -> float:
        """Convert raw SMPS parser ticks to wall-clock seconds."""
        ticks_per_sec = (self.config.target_bpm * self._effective_tpr
                         / (self.config.target_speed * 2.5))
        return ticks / ticks_per_sec if ticks_per_sec > 0 else 0.0

    def _set_cursor(self, pattern: int, channel: int, row: int) -> None:
        """Position the MOD file cursor at (pattern, channel, row)."""
        self.mod.set_active_pattern(pattern)
        self.mod.set_channel(channel)
        self.mod.set_row(row)

    def _install_synthesized_samples(self, samples_dict: dict, sample_list, prefix: str) -> None:
        """Install synthesized PCM samples into mod.samples and apply sample_list overrides."""
        sl_name_map = {e[0]: e[1] for e in sample_list} if sample_list else {}
        _MAX_SAMPLE_BYTES = 65535 * 2  # MOD 16-bit word length field limit
        for inst_num, (pcm_orig, _) in samples_dict.items():
            pcm_data = pcm_orig[:_MAX_SAMPLE_BYTES] if len(pcm_orig) > _MAX_SAMPLE_BYTES else pcm_orig
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

    def _max_note_duration_secs(self, channel_types: set) -> float:
        """Return max effective synthesis duration (seconds) across channels of given types.

        For rooted FM voice_map entries the Amiga plays the sample at a pitch-shifted
        rate whenever the note is above the root.  Lower MOD period = higher playback
        rate = sample consumed faster.  Each note's raw tick duration is therefore
        scaled by root_period / note_period so that the synthesised sample is long
        enough to cover the full note at its actual playback rate.

        Regular rests (is_no_attack=False) emit C00 (mute) in the MOD and the next
        note always restarts the sample from byte 0, so they do not add to the
        required length.  smpsNoAttack continuations (is_no_attack=True) emit no C00
        and the sample keeps advancing — these are included in the ring duration.
        """
        # Build ordered source-name lists to match SmpsChannels → ChannelConfigs.
        fm_sources  = [c.source for c in self.config.channels if c.source.startswith('FM')]
        psg_sources = [c.source for c in self.config.channels if c.source.startswith('PSG')]
        fm_idx = psg_idx = 0

        max_needed_secs = 0.0

        for ch in self.song.channels:
            ch_type = ch.header.channel_type
            # Track the source name (e.g. "FM3") for CIM lookup, regardless of filter.
            ch_source: str | None = None
            if ch_type == 'FM' and fm_idx < len(fm_sources):
                ch_source = fm_sources[fm_idx]
                fm_idx += 1
            elif ch_type == 'PSG' and psg_idx < len(psg_sources):
                ch_source = psg_sources[psg_idx]
                psg_idx += 1

            if ch_type not in channel_types:
                continue

            is_fm = (ch_type == 'FM')

            # Walk all events in order, tracking voice changes.
            current_voice = ch.header.voice
            note_events_with_voice: list[tuple] = []
            for ev in ch.events:
                if ev.is_effect and ev.effect.effect_type == 'smpsSetvoice':
                    current_voice = ev.effect.params[0]
                elif ev.is_note:
                    note_events_with_voice.append((ev, current_voice))

            for i, (ev, voice) in enumerate(note_events_with_voice):
                if ev.note.is_rest:
                    continue

                # Sum note duration + any immediately following smpsNoAttack
                # continuations (sample keeps advancing, no C00 fired).
                ring_ticks = ev.note.duration
                for j in range(i + 1, len(note_events_with_voice)):
                    nxt_ev, _ = note_events_with_voice[j]
                    if nxt_ev.note.is_rest and nxt_ev.note.is_no_attack:
                        ring_ticks += nxt_ev.note.duration
                    else:
                        break

                ring_secs = self._ticks_to_secs(ring_ticks)

                # For rooted FM entries: scale by root_period / note_period.
                # The sample is synthesised at target_rate = amiga_clock/(2×root_period).
                # When played at a higher note (lower period) it runs faster, so the
                # synthesis must be proportionally longer.
                if is_fm:
                    source_semitone = ev.note.note_value - 0x81
                    vm = self.config.voice_map.get(voice, [])
                    if ch_source and ch_source in self.config.channel_instrument_map:
                        cim_ranges = self.config.channel_instrument_map[ch_source].get(voice)
                        if cim_ranges is not None:
                            vm = cim_ranges
                    entry = next(
                        (e for e in vm if e.root is not None
                         and e.low <= source_semitone <= e.high),
                        None,
                    )
                    if entry is not None:
                        root_period = PERIOD_TABLE[entry.root.value]
                        out_note = max(0, min(
                            entry.root.value + (source_semitone - entry.low),
                            len(PERIOD_TABLE) - 2,
                        ))
                        note_period = PERIOD_TABLE[out_note]
                        if root_period > 0 and note_period > 0:
                            ring_secs *= root_period / note_period

                if ring_secs > max_needed_secs:
                    max_needed_secs = ring_secs

        return max_needed_secs

    def convert(self):
        """Main entry point. Returns a ModFile."""
        self.mod.set_name(self.config.name)

        for issue in rate3_synth_root_issues(self.config):
            self._add_warning({'type': 'rate3_synth_root', 'extra_ctx': issue['context'], **issue})

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

        # Resolve 'auto' sustain durations by scanning parsed note events
        synth = self.synth
        if synth and synth.sustain_duration == "auto":
            secs = min(self._max_note_duration_secs({'FM'}), 10.0)
            if secs > 0:
                # Scale for the highest positive finetune on any FM-synthesized instrument.
                # Positive finetune → lower ProTracker period → faster sample playback →
                # the sample runs out before the note ends without this compensation.
                _fm_insts: set[int] = {
                    e.mod_instrument
                    for ranges in self.config.voice_map.values()
                    for e in ranges
                }
                for _cim in self.config.channel_instrument_map.values():
                    for _ranges in _cim.values():
                        _fm_insts.update(e.mod_instrument for e in _ranges)
                _ft_max = 0
                if self.config.sample_list:
                    for _sl in self.config.sample_list:
                        if _sl[0] in _fm_insts and len(_sl) > 3 and _sl[3] > 0:
                            _ft_max = max(_ft_max, _sl[3])
                if _ft_max > 0:
                    # Each finetune step = 1/8 semitone = 1/96 octave.
                    # Speed factor = 2^(ft/96); compensate by extending sustain.
                    secs = min(secs * (2.0 ** (_ft_max / 96.0)), 10.0)
                synth = dataclasses.replace(synth, sustain_duration=secs)
                self._infos.append({'type': 'auto_sustain_fm', 'secs': round(secs, 3)})

        psg_synth = self.psg_synth
        if psg_synth and psg_synth.sustain_duration == "auto":
            secs = min(self._max_note_duration_secs({'PSG'}), 10.0)
            if secs > 0:
                psg_synth = dataclasses.replace(psg_synth, sustain_duration=secs)
                self._infos.append({'type': 'auto_sustain_psg', 'secs': round(secs, 3)})

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
            fm_samples = generate_fm_samples(self.song, self.config, synth)
            self._infos.append({'type': 'fm_synthesized', 'count': len(fm_samples)})
            self._install_synthesized_samples(fm_samples, self.config.sample_list, "fm")
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
                    self._infos.append({'type': 'rate3_divider', 'instrument': inst, **d})
            psg_samples = generate_psg_samples(
                self.config, psg_synth, rate3_dividers={i: d['n'] for i, d in rate3.items()})
            self._install_synthesized_samples(psg_samples, self.config.sample_list, "psg")
            self._infos.append({'type': 'psg_synthesized', 'count': len(psg_samples)})

        # Set timing
        self.mod.set_bpm(self.config.target_bpm)
        if self.config.target_speed != 6:
            self.mod.set_speed(self.config.target_speed)

        # Extend channels whose loop body is too short to cover the full song
        self._extend_looping_channels()
        self._tempo_segments = self._collect_tempo_segments()
        for ch in self.song.channels:
            if any(ev.is_effect and ev.effect.effect_type == 'smpsSetTempoDiv' for ev in ch.events):
                self._add_warning({'type': 'tempo_div_unsupported', 'channel': ch.header.label})
                break

        # Convert channels
        self._convert_all_channels()
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

            self._infos.append({
                'type': 'loop_extended',
                'label': ch.header.label,
                'from': original_count,
                'to': len(ch.events),
                'span': loop_span,
            })

    def _source_map(self) -> dict:
        """Source name ("DAC", "FM1"…, "PSG1"…) -> parsed channel, in header order."""
        source_map, fm_idx, psg_idx = {}, 0, 0
        for ch in self.song.channels:
            ch_type = ch.header.channel_type
            if ch_type == "DAC":
                source_map["DAC"] = ch
            elif ch_type == "FM":
                fm_idx += 1
                source_map[f"FM{fm_idx}"] = ch
            elif ch_type == "PSG":
                psg_idx += 1
                source_map[f"PSG{psg_idx}"] = ch
        return source_map

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
        from sfx.tables import PSG_FREQUENCIES_EXTENDED, psg_note_index

        seen: dict[int, dict] = {}        # instrument -> {'entry', 'notes': {(note_value, transpose): count}}
        source_map = self._source_map()
        for chan_cfg in self.config.channels:
            channel = source_map.get(chan_cfg.source)
            if not chan_cfg.enabled or channel is None or channel.header.channel_type != "PSG":
                continue
            transpose = channel.header.pitch_offset
            entries = self.config.psg_voice_map.get(channel.header.psg_voice_label)
            entry = entries[0] if entries else None
            for event in channel.events:
                if event.is_effect:
                    eff = event.effect
                    if eff.effect_type == 'smpsChangeTransposition':
                        transpose += eff.params[0]
                    elif eff.effect_type == 'smpsPSGform':
                        found = self.config.psg_map.get(eff.params[0])
                        if found is not None:
                            entry, entries = found, None
                    elif eff.effect_type == 'smpsPSGvoice':
                        found = self.config.psg_voice_map.get(eff.params[0])
                        if found is not None:
                            entry, entries = found[0], found
                elif event.is_note and not event.note.is_rest and entry is not None:
                    e = _psg_range_entry(entries, event.note.note_value - 0x81) or entry
                    if e.type != "tone" and e.noise_rate == 3:
                        rec = seen.setdefault(e.mod_instrument, {'entry': e, 'notes': {}})
                        key = (event.note.note_value, transpose)
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
            n = max(1, PSG_FREQUENCIES_EXTENDED[psg_note_index(note_value, transpose)])
            out[inst] = {'n': n, 'note': _semitone_to_name(note_value - 0x81), 'transpose': transpose,
                         'used': e.tone2_n is None and e.synth_root is None}
        return out

    def _fm_range_entry(self, source: str, voice_idx, source_semitone: int):
        """voice_map / channel_instrument_map entry covering this source note, or None."""
        ranges = (self.config.channel_instrument_map.get(source, {}).get(voice_idx)
                  or self.config.voice_map.get(voice_idx))
        for entry in ranges or ():
            if entry.low <= source_semitone <= entry.high:
                return entry
        return None

    def _fm_level_db(self, tl_offset: int, hard_panned: bool) -> float:
        """Hardware level of an FM note relative to TL offset 0, centred."""
        pan = self.synth.fm_pan_law_db if self.synth else 3.0
        return -_TL_STEP_DB * tl_offset - (pan if hard_panned else 0.0)

    def _plan_psg_levels(self, source_map: dict) -> dict[int, float]:
        """PSG counterpart of _plan_fm_levels: level = −2 dB × attenuation (smpsHeaderPSG volume +
        smpsPSGAlterVol).  Instrument tracking mirrors _convert_channel: header voice label,
        smpsPSGform → psg_map, smpsPSGvoice → psg_voice_map with per-note range dispatch.
        Notes at attenuation 15 are silent and do not vote.
        """
        counts: dict[int, dict[float, int]] = {}
        for chan_cfg in self.config.channels:
            channel = source_map.get(chan_cfg.source)
            if not chan_cfg.enabled or channel is None or channel.header.channel_type != "PSG":
                continue
            att, instrument = channel.header.volume, chan_cfg.instrument
            entries = self.config.psg_voice_map.get(channel.header.psg_voice_label)
            if entries:
                instrument = entries[0].mod_instrument
            for event in channel.events:
                if event.is_effect:
                    eff = event.effect
                    if eff.effect_type == 'smpsAlterVol':
                        att = max(0, min(15, att + eff.params[0]))
                    elif eff.effect_type == 'smpsPSGform':
                        entry = self.config.psg_map.get(eff.params[0])
                        if entry is not None:
                            instrument, entries = entry.mod_instrument, None
                    elif eff.effect_type == 'smpsPSGvoice':
                        found = self.config.psg_voice_map.get(eff.params[0])
                        if found is not None:
                            instrument, entries = found[0].mod_instrument, found
                elif event.is_note and not event.note.is_rest and att < 15:
                    ranged = _psg_range_entry(entries, event.note.note_value - 0x81)
                    inst = ranged.mod_instrument if ranged is not None else instrument
                    per_level = counts.setdefault(inst, {})
                    level = -_PSG_STEP_DB * att
                    per_level[level] = per_level.get(level, 0) + 1
        return {inst: max(levels, key=lambda lv: (levels[lv], lv)) for inst, levels in counts.items()}

    def _plan_fm_levels(self, source_map: dict) -> dict[int, float]:
        """"baked" volume mode: the level (dB) each MOD instrument's sample_list volume stands for.

        Walks every enabled FM channel tracking the TL offset (smpsHeaderFM volume + smpsAlterVol)
        and the pan, and counts notes per (instrument, level).  The level with the most notes is
        the instrument's baseline — those notes need no Cxx.  Ties go to the louder level so the
        others are attenuated rather than boosted past 64.
        """
        counts: dict[int, dict[float, int]] = {}
        for chan_cfg in self.config.channels:
            channel = source_map.get(chan_cfg.source)
            if not chan_cfg.enabled or channel is None or channel.header.channel_type != "FM":
                continue
            tl, hard, voice, instrument = channel.header.volume, False, None, chan_cfg.instrument
            for event in channel.events:
                if event.is_effect:
                    eff = event.effect
                    if eff.effect_type == 'smpsSetvoice':
                        voice = eff.params[0]
                        instrument = self.config.legacy_voice_map.get(voice, instrument)
                    elif eff.effect_type == 'smpsAlterVol':
                        tl = max(0, min(127, tl + eff.params[0]))
                    elif eff.effect_type == 'smpsPan':
                        hard = _pan_is_hard(eff.params)
                elif event.is_note and not event.note.is_rest and not event.note.is_dac:
                    entry = self._fm_range_entry(chan_cfg.source, voice, event.note.note_value - 0x81)
                    inst = entry.mod_instrument if entry is not None else instrument
                    per_level = counts.setdefault(inst, {})
                    level = self._fm_level_db(tl, hard)
                    per_level[level] = per_level.get(level, 0) + 1
        return {inst: max(levels, key=lambda lv: (levels[lv], lv)) for inst, levels in counts.items()}

    def _convert_all_channels(self):
        """Convert all SMPS channels to MOD channels."""
        # Source names follow header order: DAC (if present), then FM1..FMn, then PSG1..PSGn
        source_map = self._source_map()

        self._fm_baseline_db: dict[int, float] = {}
        if self._fm_volume_mode == "baked":
            self._fm_baseline_db = self._plan_fm_levels(source_map)
        self._psg_baseline_db: dict[int, float] = {}
        if self._psg_volume_mode == "baked":
            self._psg_baseline_db = self._plan_psg_levels(source_map)

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
        instrument = chan_cfg.instrument
        volume = chan_cfg.volume
        transpose = chan_cfg.transpose + channel.header.pitch_offset

        # Per-channel state
        current_volume = volume
        current_voice_idx = None
        note_fill = 0
        vibrato_active = False
        vibrato_speed = 0
        vibrato_change = 0   # raw SMPS delta byte (FNUM / PSG divider units); scaled per note
        vibrato_steps = 0    # raw SMPS steps byte
        vibrato_wait = 0   # ticks to delay before vibrato starts
        current_psg_entry = None    # active PsgInstrumentEntry for the current note (range-dispatched)
        current_psg_entries = None  # full list[PsgInstrumentEntry] for the active psg_voice_map label
        current_psg_label = None    # label string for warnings (e.g. "fTone_01", "form 0xe7")
        active_range_entry = None  # voice_map InstrumentRange matched on most recent note

        # Apply initial PSG voice from smpsHeaderPSG if present and mapped
        _init_psg_label = channel.header.psg_voice_label
        if _init_psg_label and _init_psg_label in self.config.psg_voice_map:
            _init_entries = self.config.psg_voice_map[_init_psg_label]
            instrument = _init_entries[0].mod_instrument
            current_psg_entry = _init_entries[0]
            current_psg_entries = _init_entries
            current_psg_label = _init_psg_label

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

        # PSG attenuation state (0-15, 2 dB/step).  Initialized from the
        # smpsHeaderPSG volume byte; updated on smpsPSGAlterVol events.
        psg_attenuation: int = 0
        if is_psg:
            psg_attenuation = channel.header.volume
            current_volume = round(_psg_att_to_mod(psg_attenuation) * chan_cfg.volume / 64)

        # FM TL-offset state (0-127, 0.75 dB/step).  Initialized from the
        # smpsHeaderFM initial_vol byte; updated on smpsAlterVol events.
        # How it reaches the MOD depends on the fm_volume_scaling mode.
        _fm_mode = self._fm_volume_mode
        _fm_vol_scaling = _fm_mode == "absolute"
        _fm_baked = _fm_mode == "baked" and not is_psg and not is_dac
        fm_tl_offset: int = 0
        fm_hard_panned = False
        if not is_psg and not is_dac and _fm_mode != "off":
            fm_tl_offset = channel.header.volume
            if _fm_vol_scaling:
                current_volume = round(_fm_tl_to_mod(fm_tl_offset) * chan_cfg.volume / 64)

        _psg_baked = is_psg and self._psg_volume_mode == "baked"

        def _emit_volume(inst: int) -> int:
            """MOD volume for a note on `inst` right now (equals the sample volume → no Cxx)."""
            sv = _sample_vol_map.get(inst, 64)
            if _psg_baked:
                if psg_attenuation >= 15:
                    return 0
                level = -_PSG_STEP_DB * psg_attenuation
                rel_db = level - self._psg_baseline_db.get(inst, level)
                return max(0, min(64, round(sv * 10 ** (rel_db / 20.0) * chan_cfg.volume / 64)))
            if not _fm_baked:
                return round(current_volume * sv / 64)
            level = self._fm_level_db(fm_tl_offset, fm_hard_panned)
            rel_db = level - self._fm_baseline_db.get(inst, level)
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
            # A row is tpr / _ticks_per_frame frames and `speed` MOD ticks long.
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

        for event in channel.events:
            if event.is_effect:
                eff = event.effect

                if eff.effect_type == 'smpsSetvoice':
                    voice_idx = eff.params[0]
                    current_voice_idx = voice_idx
                    if voice_idx in self.config.legacy_voice_map:
                        instrument = self.config.legacy_voice_map[voice_idx]

                elif eff.effect_type == 'smpsAlterVol':
                    delta = eff.params[0]
                    if is_psg:
                        psg_attenuation = max(0, min(15, psg_attenuation + delta))
                        current_volume = round(_psg_att_to_mod(psg_attenuation) * chan_cfg.volume / 64)
                    elif _fm_vol_scaling:
                        fm_tl_offset = max(0, min(127, fm_tl_offset + delta))
                        current_volume = round(_fm_tl_to_mod(fm_tl_offset) * chan_cfg.volume / 64)
                    elif _fm_baked:
                        fm_tl_offset = max(0, min(127, fm_tl_offset + delta))
                    else:
                        current_volume = max(0, min(64, current_volume - delta))

                elif eff.effect_type == 'smpsPan':
                    fm_hard_panned = _pan_is_hard(eff.params)

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

                elif eff.effect_type == 'smpsChangeTransposition':
                    transpose += eff.params[0]

                elif eff.effect_type == 'smpsPSGform':
                    form_byte = eff.params[0]
                    psg_entry = self.config.psg_map.get(form_byte)
                    if psg_entry is not None:
                        instrument = psg_entry.mod_instrument
                        current_psg_entry = psg_entry
                        current_psg_entries = None          # smpsPSGform is not a voice-map event
                        current_psg_label = f"form {form_byte:#04x}"

                elif eff.effect_type == 'smpsPSGvoice':
                    label = eff.params[0]
                    entries = self.config.psg_voice_map.get(label)
                    if entries is not None:
                        instrument = entries[0].mod_instrument
                        current_psg_entry = entries[0]
                        current_psg_entries = entries
                        current_psg_label = label

                # smpsPan, smpsNop: no MOD equivalent
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
                    # Skip C00 at pattern 0 row 0 — nothing is playing yet and
                    # that cell holds the speed/BPM command.
                    if pattern < self.config.max_patterns and (pattern > 0 or row > 0):
                        while pattern >= len(self.mod.patterns):
                            self.mod.add_patterns(1)
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

                # Ensure enough patterns exist
                while pattern >= len(self.mod.patterns):
                    self.mod.add_patterns(1)

                self._set_cursor(pattern, mod_chan, row)
                note_delay = 0

                if is_dac:
                    # DAC notes carry no other effect, so the slot is always free for EDx.
                    pattern, row, note_delay = _note_cell(tick, True, None)
                    if pattern >= self.config.max_patterns:
                        break
                    while pattern >= len(self.mod.patterns):
                        self.mod.add_patterns(1)
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
                        self.mod.set_note(ModNote.C3, instrument)
                    if note_delay:
                        self.mod.set_effect(0xE, 0xD0 | note_delay)
                else:
                    # Melodic: place note with optional voice_map override.
                    #
                    # The map is checked against the *source semitone* — the raw
                    # SMPS note + smpsAlterNote, before the channel base transpose.
                    # This matches the mml2mod reference design: ranges are defined
                    # in source-note space, root anchors the output to a MOD note.
                    total_transpose = transpose
                    source_semitone = (note.note_value - 0x81)

                    final_instrument = instrument
                    final_note = None
                    active_range_entry = None  # reset on each note

                    # Channel-specific override takes priority over global voice_map
                    # (same lookup as the level pre-pass in _plan_fm_levels).
                    ranges = (self.config.channel_instrument_map.get(chan_cfg.source, {}).get(current_voice_idx)
                              or self.config.voice_map.get(current_voice_idx))   # for the map_gap warning
                    entry = self._fm_range_entry(chan_cfg.source, current_voice_idx, source_semitone)
                    if entry is not None:
                        active_range_entry = entry
                        final_instrument = entry.mod_instrument
                        if entry.root is not None:
                            out_raw = entry.root.value + (source_semitone - entry.low)
                            out = max(0, min(35, out_raw))
                            if out != out_raw:
                                self._add_warning({
                                    'type': 'clamp_high' if out_raw > 35 else 'clamp_low',
                                    'channel': chan_cfg.source,
                                    'voice_idx': current_voice_idx,
                                    'src_name': _semitone_to_name(source_semitone),
                                    'boundary': _semitone_to_name(entry.high if out_raw > 35 else entry.low),
                                    'note_value': note.note_value,
                                    'transpose': 0,
                                })
                            final_note = ModNote(out)
                        # root=None: fall through to channel-transpose path

                    if final_note is None:
                        # Per-note range dispatch for multi-entry psg_voice_map lists.
                        # Mirrors voice_map FM dispatch: pick the entry whose low/high bracket
                        # contains the source semitone, update instrument accordingly.
                        _psg_e = _psg_range_entry(current_psg_entries, source_semitone)
                        if _psg_e is not None:
                            current_psg_entry = _psg_e
                            final_instrument = _psg_e.mod_instrument

                        # PSG root anchoring: bypass the transpose path entirely when a
                        # psg_map/psg_voice_map entry is active — avoids spurious out-of-range
                        # warnings for noise channels whose SMPS note bytes carry no pitch meaning.
                        # For melodic tones with low set, apply the same root-offset formula as
                        # InstrumentRange. synth_root is synthesis-only; the MOD trigger note
                        # is determined by root (+/- offset from low).
                        psg_anchor = None
                        if current_psg_entry is not None and current_psg_entry.root is not None:
                            if current_psg_entry.low is not None:
                                # Melodic anchor: root + (source − low), clamped to MOD range
                                psg_out_raw = current_psg_entry.root.value + (source_semitone - current_psg_entry.low)
                                psg_out = max(0, min(35, psg_out_raw))
                                if psg_out != psg_out_raw:
                                    self._add_warning({
                                        'type': 'clamp_high' if psg_out_raw > 35 else 'clamp_low',
                                        'channel': chan_cfg.source,
                                        'voice_idx': None,
                                        'extra_ctx': current_psg_label,
                                        'src_name': _semitone_to_name(source_semitone),
                                        'boundary': _semitone_to_name(
                                            current_psg_entry.low + (35 - current_psg_entry.root.value)
                                        ),
                                        'note_value': note.note_value,
                                        'transpose': 0,
                                    })
                                psg_anchor = ModNote(psg_out)
                            elif current_psg_entry.type != "tone":
                                # Fixed anchor: noise channels (no pitch content)
                                psg_anchor = current_psg_entry.root
                            # tone with root but no low → psg_anchor stays None → transpose path
                        if psg_anchor is not None:
                            final_note = psg_anchor
                        else:
                            # Warn if a voice_instrument_map entry exists for this voice but
                            # the note fell outside every defined range — almost always a
                            # config gap rather than intentional fallback.
                            if ranges and current_voice_idx is not None:
                                note_name = _semitone_to_name(source_semitone)
                                range_lo  = _semitone_to_name(ranges[0].low)
                                range_hi  = _semitone_to_name(ranges[-1].high)
                                self._add_warning({
                                    'type': 'map_gap',
                                    'channel': chan_cfg.source,
                                    'voice_idx': current_voice_idx,
                                    'extra_ctx': current_psg_label,
                                    'note_name': note_name,
                                    'semitone': source_semitone,
                                    'range_lo': range_lo,
                                    'range_hi': range_hi,
                                })
                            # No map match (or matched with no root): use channel transpose
                            # Wrap warn_fn to inject psg_voice_map label list when the
                            # active PSG label is unknown (note fired before smpsPSGvoice).
                            _psg_label = current_psg_label
                            _psg_labels = (
                                list(self.config.psg_voice_map.keys())
                                if chan_cfg.source.startswith('PSG')
                                   and not _psg_label
                                   and self.config.psg_voice_map
                                else None
                            )
                            def _warn_psg(w, _lbl=_psg_label, _lbls=_psg_labels):
                                if _lbls:
                                    w['psg_available_labels'] = _lbls
                                self._add_warning(w)
                            final_note = smps_note_to_mod_note(
                                note.note_value, total_transpose, chan_cfg.source,
                                voice_idx=current_voice_idx,
                                warn_fn=_warn_psg,
                                extra_ctx=_psg_label)

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
                    while pattern >= len(self.mod.patterns):
                        self.mod.add_patterns(1)
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
                            while fill_pat >= len(self.mod.patterns):
                                self.mod.add_patterns(1)
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
                            while cut_pat >= len(self.mod.patterns):
                                self.mod.add_patterns(1)
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
                        stride = self.mod.CHANNELS * 4
                        end_pat, end_row = self._tick_to_pattern_row(tick + note.duration)
                        r_total = pattern * 64 + row + 1
                        while r_total < end_pat * 64 + end_row and r_total // 64 < self.config.max_patterns:
                            p_, r_ = divmod(r_total, 64)
                            while p_ >= len(self.mod.patterns):
                                self.mod.add_patterns(1)
                            cell = self.mod.patterns[p_].get_bytes()
                            i = mod_chan * 4 + r_ * stride
                            if (cell[i + 2] & 0x0F) == 0 and cell[i + 3] == 0:
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
                    elif current_psg_entry is not None and current_psg_entry.vibrato is not None:
                        _vib_override = current_psg_entry.vibrato

                    if _vib_override is not None:
                        eff_vib_speed = (_vib_override >> 4) & 0xF
                        eff_vib_depth = _vib_override & 0xF
                    else:
                        eff_vib_speed = vibrato_speed
                        # Depth is per note: the driver's swing is a fixed number of FNUM / divider
                        # units, so its size in cents depends on the chip note it is added to.
                        eff_vib_depth = self._vibrato_depth(
                            vibrato_change, vibrato_steps, PERIOD_TABLE[final_note.value],
                            source_semitone + transpose - chan_cfg.transpose, is_psg)
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
        label_tick_pos = self.song.label_tick_pos
        loop_target_tick = None

        for ch in self.song.channels:
            if ch.has_jump and ch.jump_target_label:
                tick = label_tick_pos.get(ch.jump_target_label)
                if tick is not None and (loop_target_tick is None or tick > loop_target_tick):
                    loop_target_tick = tick

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
        song_end_flat = max(round(song_end_tick / tpr), 1) - 1

        if breaks:
            for P, break_row in sorted(breaks):
                body_start = P * 64 + break_row + 1
                if song_end_flat >= body_start:
                    song_end_flat += 63 - break_row
        last_pattern = song_end_flat // 64
        last_row     = song_end_flat % 64

        # Target: map loop_target_tick to post-break (pattern, row)
        flat_row = round(loop_target_tick / tpr)

        if breaks:
            for P, break_row in sorted(breaks):
                body_start = P * 64 + break_row + 1
                if flat_row >= body_start:
                    flat_row += 63 - break_row
        target_pattern = flat_row // 64
        target_row = flat_row % 64

        self._set_cursor(last_pattern, 0, last_row)
        self.mod.set_position_jump(target_pattern)

        # A song that loops back into a different tempo segment needs its BPM set again there
        # (the Fxx cells written by _write_tempo_changes sit at the changes, not at the target).
        segs = getattr(self, '_tempo_segments', None) or []
        if len(segs) > 1 and self._segment_at(loop_target_tick)[1] != segs[-1][1]:
            target_mod = self._segment_at(loop_target_tick)[1]
            stride = self.mod.CHANNELS * 4
            pat_data = self.mod.patterns[target_pattern].get_bytes()
            for ch in range(self.mod.CHANNELS):
                i = ch * 4 + target_row * stride
                if (pat_data[i + 2] & 0x0F) == 0 and pat_data[i + 3] == 0:
                    self._set_cursor(target_pattern, ch, target_row)
                    self.mod.set_effect(0xF, self._bpm_for(target_mod))
                    break
            else:
                self._add_warning({'type': 'tempo_no_slot', 'channel': 'all', 'tick': loop_target_tick,
                                   'pattern': target_pattern, 'row': target_row, 'modifier': target_mod,
                                   'bpm': self._bpm_for(target_mod), 'exact_bpm': float('nan')})

        # If the target lands mid-pattern, write a Dxx companion on a free channel
        if target_row != 0:
            bcd = ((target_row // 10) << 4) | (target_row % 10)
            stride = self.mod.CHANNELS * 4
            pat_data = self.mod.patterns[last_pattern].get_bytes()
            for ch in range(1, self.mod.CHANNELS):
                didx = ch * 4 + last_row * stride
                if (pat_data[didx + 2] & 0xF) == 0 and pat_data[didx + 3] == 0:
                    self.mod.set_channel(ch)
                    self.mod.set_effect(0xD, bcd)
                    break

        self._infos.append({
            'type': 'loop_set',
            'pattern': last_pattern,
            'row': last_row,
            'target': target_pattern,
        })
