"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.
"""

from .tables import ModNote, smps_note_to_mod_note, SMPS_DAC_NAMES, _semitone_to_name
from .mod import ModFile, ModSample
from .smps_parser import SmpsSong, SmpsChannel, SmpsEvent, SmpsNote, SmpsEffect
from .config import ConversionConfig, ChannelConfig, DacSampleConfig, InstrumentRange, SynthesisSettings, PsgSynthesisSettings


# Map MOD note name strings to ModNote enum values
# Supports both "#" (F#3) and "s" (Fs3) sharp notation, plus "b" for flats
_MOD_NOTE_MAP = {}
for _oct in range(1, 4):
    _notes = [
        (f"C{_oct}", f"C{_oct}"),
        (f"C#{_oct}", f"C{_oct}s"), (f"Cs{_oct}", f"C{_oct}s"), (f"Db{_oct}", f"C{_oct}s"),
        (f"D{_oct}", f"D{_oct}"),
        (f"D#{_oct}", f"D{_oct}s"), (f"Ds{_oct}", f"D{_oct}s"), (f"Eb{_oct}", f"D{_oct}s"),
        (f"E{_oct}", f"E{_oct}"),
        (f"F{_oct}", f"F{_oct}"),
        (f"F#{_oct}", f"F{_oct}s"), (f"Fs{_oct}", f"F{_oct}s"), (f"Gb{_oct}", f"F{_oct}s"),
        (f"G{_oct}", f"G{_oct}"),
        (f"G#{_oct}", f"G{_oct}s"), (f"Gs{_oct}", f"G{_oct}s"), (f"Ab{_oct}", f"G{_oct}s"),
        (f"A{_oct}", f"A{_oct}"),
        (f"A#{_oct}", f"A{_oct}s"), (f"As{_oct}", f"A{_oct}s"), (f"Bb{_oct}", f"A{_oct}s"),
        (f"B{_oct}", f"B{_oct}"),
    ]
    for _key, _enum_name in _notes:
        _MOD_NOTE_MAP[_key] = ModNote[_enum_name]
del _oct, _notes, _key, _enum_name


class SmpsToModConverter:
    def __init__(self, song: SmpsSong, config: ConversionConfig,
                 synth: SynthesisSettings = None,
                 psg_synth: PsgSynthesisSettings = None):
        self.song = song
        self.config = config
        self.synth = synth
        self.psg_synth = psg_synth
        self.mod = ModFile(channels=config.num_mod_channels)

    def convert(self):
        """Main entry point. Returns a ModFile."""
        self.mod.set_name(self.config.name)

        # Collect PSG instrument numbers that will be synthesized so disk loading
        # can skip them (avoids spurious "file not found" warnings).
        psg_synth_insts: set = set()
        if self.psg_synth and self.psg_synth.enabled and self.config.psg_map:
            psg_synth_insts = {e.mod_instrument for e in self.config.psg_map.values()}

        # Load or synthesize samples
        synth = self.synth
        if synth and synth.enabled and synth.mode == "ym2612":
            from ym2612.sample_generator import generate_fm_samples
            print("  Synthesizing FM samples...")
            fm_samples = generate_fm_samples(self.song, self.config, synth)
            # Install synthesized FM samples
            for inst_num, (pcm, rate) in fm_samples.items():
                sample = ModSample(f"fm_inst{inst_num}")
                sample.data = pcm
                sample.length = len(pcm) // 2
                sample.set_volume(64)
                self.mod.samples[inst_num - 1] = sample
            # Apply volume and finetune from sample_list to synthesized samples
            if self.config.sample_list:
                for entry in self.config.sample_list:
                    inst_num_sl = entry[0]
                    if inst_num_sl in fm_samples:
                        vol_sl = entry[2] if len(entry) > 2 else 64
                        ft_sl  = entry[3] if len(entry) > 3 else 0
                        self.mod.samples[inst_num_sl - 1].set_volume(vol_sl)
                        if ft_sl != 0:
                            self.mod.samples[inst_num_sl - 1].set_finetune(ft_sl)
            # Load remaining (DAC) samples from disk — skip FM-synthesized and PSG-synthesized instruments
            if self.config.sample_list:
                for entry in self.config.sample_list:
                    inst_num = entry[0]
                    if inst_num not in fm_samples and inst_num not in psg_synth_insts:
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
        psg_synth = self.psg_synth
        if psg_synth and psg_synth.enabled and self.config.psg_map:
            from sn76489.sample_generator import generate_psg_samples
            print("  Synthesizing PSG samples...")
            psg_samples = generate_psg_samples(self.config, psg_synth)
            for inst_num, (pcm, rate) in psg_samples.items():
                sample = ModSample(f"psg_inst{inst_num}")
                sample.data = pcm
                sample.length = len(pcm) // 2
                sample.set_volume(64)
                sample.set_finetune(0)
                self.mod.samples[inst_num - 1] = sample
            # Apply volume and finetune overrides from sample_list
            if self.config.sample_list:
                for entry in self.config.sample_list:
                    inst_num_sl = entry[0]
                    if inst_num_sl in psg_samples:
                        vol_sl = entry[2] if len(entry) > 2 else 64
                        ft_sl  = entry[3] if len(entry) > 3 else 0
                        self.mod.samples[inst_num_sl - 1].set_volume(vol_sl)
                        if ft_sl != 0:
                            self.mod.samples[inst_num_sl - 1].set_finetune(ft_sl)
            print(f"  PSG: synthesized {len(psg_samples)} instrument(s)")

        # Set timing
        self.mod.set_bpm(self.config.target_bpm)
        if self.config.target_speed != 6:
            self.mod.set_speed(self.config.target_speed)

        # Convert channels
        self._convert_all_channels()

        # Set loop point from smpsJump
        self._set_loop_point()

        return self.mod

    def _convert_all_channels(self):
        """Convert all SMPS channels to MOD channels."""
        # Build a map from source name to parsed channel
        source_map = {}
        header_channels = self.song.header.channels

        # Assign source names based on header order:
        # First is DAC (if present), then FM1..FMn, then PSG1..PSGn
        dac_idx = 0
        fm_idx = 0
        psg_idx = 0

        for ch in self.song.channels:
            ch_type = ch.header.channel_type
            if ch_type == "DAC":
                source_map["DAC"] = ch
                dac_idx += 1
            elif ch_type == "FM":
                fm_idx += 1
                source_map[f"FM{fm_idx}"] = ch
            elif ch_type == "PSG":
                psg_idx += 1
                source_map[f"PSG{psg_idx}"] = ch

        # Convert each configured channel
        for chan_cfg in self.config.channels:
            if not chan_cfg.enabled:
                continue

            source = chan_cfg.source
            if source not in source_map:
                print(f"Warning: Source '{source}' not found in parsed song")
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
        alter_note = 0
        note_fill = 0
        vibrato_active = False
        vibrato_speed = 0
        vibrato_depth = 0

        # Build DAC name -> config map
        dac_map = {}
        for dac_cfg in self.config.dac_samples:
            dac_map[dac_cfg.name] = dac_cfg

        # Build {inst_num: sample_vol} from sample_list for Cxx scaling
        _sample_vol_map = {}
        if self.config.sample_list:
            for sl_entry in self.config.sample_list:
                _sample_vol_map[sl_entry[0]] = sl_entry[2] if len(sl_entry) > 2 else 64

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
                    current_volume = max(0, min(64, current_volume - delta))

                elif eff.effect_type == 'smpsAlterNote':
                    alter_note = eff.params[0]

                elif eff.effect_type == 'smpsNoteFill':
                    note_fill = eff.params[0]

                elif eff.effect_type == 'smpsModSet':
                    # wait, speed, depth, steps
                    vibrato_speed = min(eff.params[1], 0xF)
                    vibrato_depth = min(eff.params[2], 0xF)
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

                elif eff.effect_type == 'smpsPSGvoice':
                    label = eff.params[0]
                    new_inst = self.config.psg_voice_map.get(label)
                    if new_inst is not None:
                        instrument = new_inst

                # smpsPan, smpsNop: no MOD equivalent
                continue

            if event.is_note:
                note = event.note
                tick = event.tick_position

                if note.is_rest:
                    pattern, row = self._tick_to_pattern_row(tick)
                    # Skip C00 at pattern 0 row 0 — nothing is playing yet and
                    # that cell holds the speed/BPM command.
                    if pattern < self.config.max_patterns and (pattern > 0 or row > 0):
                        while pattern >= len(self.mod.patterns):
                            self.mod.add_patterns(1)
                        self.mod.set_active_pattern(pattern)
                        self.mod.set_channel(mod_chan)
                        self.mod.set_row(row)
                        self.mod.set_effect(0xC, 0)  # C00: mute channel
                    continue

                # Calculate pattern/row from tick
                pattern, row = self._tick_to_pattern_row(tick)

                if pattern >= self.config.max_patterns:
                    print(f"Warning: Pattern {pattern} exceeds max_patterns ({self.config.max_patterns}), truncating")
                    break

                # Ensure enough patterns exist
                while pattern >= len(self.mod.patterns):
                    self.mod.add_patterns(1)

                self.mod.set_active_pattern(pattern)
                self.mod.set_channel(mod_chan)
                self.mod.set_row(row)

                if is_dac:
                    # DAC: look up instrument and note from dac_samples config
                    dac_cfg = dac_map.get(note.dac_name)
                    if dac_cfg:
                        dac_inst = dac_cfg.mod_instrument
                        dac_note = _MOD_NOTE_MAP.get(dac_cfg.mod_note, ModNote.C3)
                        self.mod.set_note(dac_note, dac_inst)
                    else:
                        # Fallback: use default instrument and C3
                        self.mod.set_note(ModNote.C3, instrument)
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

                    # Channel-specific override takes priority over global voice_map
                    _cim = self.config.channel_instrument_map.get(chan_cfg.source, {})
                    ranges = _cim.get(current_voice_idx) \
                          or self.config.voice_map.get(current_voice_idx)
                    if ranges:
                        for entry in ranges:
                            if entry.low <= source_semitone <= entry.high:
                                final_instrument = entry.mod_instrument
                                if entry.root is not None:
                                    out = entry.root.value + (source_semitone - entry.low)
                                    out = max(0, min(35, out))
                                    final_note = ModNote(out)
                                # root=None: fall through to channel-transpose path
                                break

                    if final_note is None:
                        # Warn if a voice_instrument_map entry exists for this voice but
                        # the note fell outside every defined range — almost always a
                        # config gap rather than intentional fallback.
                        if ranges and current_voice_idx is not None:
                            note_name = _semitone_to_name(source_semitone)
                            range_lo  = _semitone_to_name(ranges[0].low)
                            range_hi  = _semitone_to_name(ranges[-1].high)
                            print(f"Warning [{chan_cfg.source} voice={current_voice_idx}]: "
                                  f"n{note_name} (semitone {source_semitone}) not covered by "
                                  f"voice_map (spans {range_lo}–{range_hi}); "
                                  f"falling back to transpose path")
                        # No map match (or matched with no root): use channel transpose
                        final_note = smps_note_to_mod_note(
                            note.note_value, total_transpose, chan_cfg.source,
                            voice_idx=current_voice_idx)

                    self.mod.set_note(final_note, final_instrument)

                    # Note fill: silence the channel at the exact tick the driver
                    # fires PSGNoteOff/FMNoteOff.  Skip when fill >= note.duration —
                    # the hardware edge case where DurationTimeout fires before
                    # NoteTimeout so the fill timer never completes (note sustains).
                    fill_placed = False
                    if note_fill > 0 and note_fill < note.duration:
                        fill_pat, fill_row = self._tick_to_pattern_row(tick + note_fill)
                        if fill_pat == pattern and fill_row == row:
                            # Fill fires within the current row: ECx.
                            # Scale fill from SMPS ticks to MOD VBL ticks,
                            # cap at speed-1 so the effect always fires.
                            ec_val = round(
                                note_fill * self.config.target_speed
                                / self.config.ticks_per_row
                            )
                            ec_val = min(ec_val, self.config.target_speed - 1)
                            if ec_val > 0:
                                self.mod.set_effect(0xE, 0xC0 | ec_val)
                                fill_placed = True
                        elif fill_pat < self.config.max_patterns:
                            # Fill fires on a later row: write C00 there directly.
                            while fill_pat >= len(self.mod.patterns):
                                self.mod.add_patterns(1)
                            self.mod.set_active_pattern(fill_pat)
                            self.mod.set_channel(mod_chan)
                            self.mod.set_row(fill_row)
                            self.mod.set_effect(0xC, 0)
                            # Restore cursor to the current note's cell.
                            self.mod.set_active_pattern(pattern)
                            self.mod.set_channel(mod_chan)
                            self.mod.set_row(row)
                            fill_placed = True

                    if not fill_placed:
                        # Emit Cxx only when the scaled output differs from the
                        # instrument's own sample volume — MOD auto-resets to
                        # sample volume on each note trigger, so no command is
                        # needed when the volume is at its default.
                        sv = _sample_vol_map.get(final_instrument, 64)
                        emit_vol = round(current_volume * sv / 64)
                        if emit_vol != sv:
                            self.mod.set_effect(0xC, emit_vol)

                        # Vibrato effect (4xy)
                        elif vibrato_active and vibrato_speed > 0:
                            param = (vibrato_speed << 4) | vibrato_depth
                            self.mod.set_effect(0x4, param)

    def _tick_to_pattern_row(self, tick):
        """Convert a tick position to (pattern_index, row_within_pattern).

        Args:
            tick: Cumulative tick position

        Returns:
            (pattern, row) tuple
        """
        tpr = self.config.ticks_per_row
        row_total = int(round(tick / tpr))
        pattern = row_total // 64
        row = row_total % 64
        return pattern, row

    def _last_data_pattern_row(self):
        """Return (pattern, row) of the last event written across all channels.

        Scans every channel's event list and takes the maximum tick_position,
        which corresponds to the last MOD row that has actual data written to it.
        """
        last_tick = 0
        for ch in self.song.channels:
            if ch.events:
                last_tick = max(last_tick, ch.events[-1].tick_position)
        return self._tick_to_pattern_row(last_tick)

    def _set_loop_point(self):
        """Set Bxx position jump for song looping based on smpsJump targets.

        Uses the maximum loop-start tick across all channels so the jump
        target lands after every channel's intro has completed.
        """
        label_tick_pos = self.song.label_tick_pos
        loop_target_tick = None

        for ch in self.song.channels:
            if ch.has_jump and ch.jump_target_label:
                tick = label_tick_pos.get(ch.jump_target_label)
                if tick is not None:
                    if loop_target_tick is None or tick > loop_target_tick:
                        loop_target_tick = tick

        if loop_target_tick is None:
            return  # No smpsJump found; nothing to do

        last_pattern, last_row = self._last_data_pattern_row()
        target_pattern, _ = self._tick_to_pattern_row(loop_target_tick)

        self.mod.set_active_pattern(last_pattern)
        self.mod.set_channel(0)
        self.mod.set_row(last_row)
        self.mod.set_position_jump(target_pattern)
        print(f"Set loop: pattern {last_pattern} row {last_row} -> position {target_pattern}")
