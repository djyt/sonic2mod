"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.
"""

from tables import ModNote, smps_note_to_mod_note, SMPS_DAC_NAMES, _semitone_to_name
from mod import ModFile
from smps_parser import SmpsSong, SmpsChannel, SmpsEvent, SmpsNote, SmpsEffect
from config import ConversionConfig, ChannelConfig, DacSampleConfig, InstrumentRange, SynthesisSettings
from mod import ModSample


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
    def __init__(self, song: SmpsSong, config: ConversionConfig, synth: SynthesisSettings = None):
        self.song = song
        self.config = config
        self.synth = synth
        self.mod = ModFile(channels=config.num_mod_channels)

    def convert(self):
        """Main entry point. Returns a ModFile."""
        self.mod.set_name(self.config.name)

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
            # Load remaining (DAC) samples from disk if sample_list exists
            if self.config.sample_list:
                for entry in self.config.sample_list:
                    inst_num = entry[0]
                    if inst_num not in fm_samples:
                        self.mod.add_samples(self.config.samples_dir, [entry])
        elif self.config.sample_list:
            self.mod.add_samples(self.config.samples_dir, self.config.sample_list)
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

                # smpsPan, smpsNop, smpsPSGform, smpsPSGvoice: ignored
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

                    # Emit Cxx only when the scaled output differs from the
                    # instrument's own sample volume — MOD auto-resets to sample
                    # volume on each note trigger, so no command is needed when
                    # the volume is at its default.
                    sv = _sample_vol_map.get(final_instrument, 64)
                    emit_vol = round(current_volume * sv / 64)
                    if emit_vol != sv:
                        self.mod.set_effect(0xC, emit_vol)

                    # Vibrato effect (4xy)
                    elif vibrato_active and vibrato_speed > 0:
                        param = (vibrato_speed << 4) | vibrato_depth
                        self.mod.set_effect(0x4, param)

                    # Note fill → ECx (note cut)
                    elif note_fill > 0 and note_fill < 0x10:
                        self.mod.set_effect(0xE, 0xC0 | (note_fill & 0xF))

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

    def _set_loop_point(self):
        """Set Bxx position jump for song looping based on smpsJump targets."""
        # Find the first channel with a jump
        for i, ch in enumerate(self.song.channels):
            if ch.has_jump and ch.jump_target_label:
                # Look up the tick position of the jump target
                from smps_parser import SmpsParser
                # The parser stores label tick positions
                # We need to find the pattern that the jump target corresponds to
                # For now, use a simple heuristic: find the target label's tick in the channel

                # Find the last event's tick position to know where to place Bxx
                if ch.events:
                    last_tick = ch.events[-1].tick_position
                    if ch.events[-1].is_note and ch.events[-1].note:
                        last_tick += ch.events[-1].note.duration
                    last_pattern, last_row = self._tick_to_pattern_row(last_tick)

                    # Find the target tick position
                    target_tick = self._find_label_tick(ch, ch.jump_target_label)
                    if target_tick is not None:
                        target_pattern, _ = self._tick_to_pattern_row(target_tick)

                        # Place Bxx at the end of the last pattern
                        while last_pattern >= len(self.mod.patterns):
                            self.mod.add_patterns(1)

                        self.mod.set_active_pattern(last_pattern)
                        self.mod.set_channel(0)  # Place on channel 0
                        self.mod.set_row(63)     # Last row of pattern
                        self.mod.set_position_jump(target_pattern)
                        print(f"Set loop: pattern {last_pattern} row 63 -> position {target_pattern}")
                break

    def _find_label_tick(self, channel, label):
        """Find the tick position of a label in the channel's events.

        Falls back to checking the parser's label_tick_pos.
        """
        # Check events for the label's tick position
        for event in channel.events:
            if event.tick_position >= 0:
                # We don't store label info in events directly,
                # so use the parser's label_tick_pos
                pass

        # Return 0 as a fallback (loop to beginning)
        return 0
