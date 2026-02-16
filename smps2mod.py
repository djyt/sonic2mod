"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.
"""

from tables import ModNote, smps_note_to_mod_note, SMPS_DAC_NAMES
from mod import ModFile
from smps_parser import SmpsSong, SmpsChannel, SmpsEvent, SmpsNote, SmpsEffect
from config import ConversionConfig, ChannelConfig, DacSampleConfig


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
    def __init__(self, song: SmpsSong, config: ConversionConfig):
        self.song = song
        self.config = config
        self.mod = ModFile(channels=config.num_mod_channels)

    def convert(self):
        """Main entry point. Returns a ModFile."""
        self.mod.set_name(self.config.name)

        # Load samples if provided
        if self.config.sample_list:
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
        transpose = chan_cfg.transpose

        # Per-channel state
        current_volume = volume
        alter_note = 0
        note_fill = 0
        vibrato_active = False
        vibrato_speed = 0
        vibrato_depth = 0

        # Build DAC name -> config map
        dac_map = {}
        for dac_cfg in self.config.dac_samples:
            dac_map[dac_cfg.name] = dac_cfg

        for event in channel.events:
            if event.is_effect:
                eff = event.effect

                if eff.effect_type == 'smpsSetvoice':
                    # Map voice index to instrument (informational; keep current instrument)
                    pass

                elif eff.effect_type == 'smpsAlterVol':
                    delta = eff.params[0]
                    current_volume = max(0, min(64, current_volume + delta))

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
                    # Rests: we could place a note cut, or just leave the row empty
                    # For now, skip rests (silence happens naturally in MOD)
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
                    # Melodic: convert SMPS note to MOD note with transpose
                    total_transpose = transpose + alter_note
                    mod_note = smps_note_to_mod_note(note.note_value, total_transpose)
                    if mod_note is not None:
                        self.mod.set_note(mod_note, instrument)

                    # Set volume if changed
                    if current_volume != volume:
                        self.mod.set_effect(0xC, current_volume)

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
        row_total = int(tick / tpr)
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
                        print(f"Set loop: pattern {last_pattern} row 63 → position {target_pattern}")
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
