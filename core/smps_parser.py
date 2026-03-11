"""SMPS assembly music parser.

Parses Sonic 1 SMPS assembly files (macro-based note data) into an
intermediate representation suitable for conversion to MOD format.
"""

import re
from dataclasses import dataclass, field

from .tables import SMPS_DAC_NAMES, SMPS_NOTE_NAMES

# ---------------------------------------------------------------------------
# Intermediate representation data classes
# ---------------------------------------------------------------------------

@dataclass
class SmpsNote:
    note_value: int       # SMPS byte value (0x80=rest, 0x81=C0, etc.)
    duration: int         # Duration in ticks
    is_rest: bool = False
    is_dac: bool = False
    dac_name: str = ""    # Original DAC name (e.g. "dKick")
    is_no_attack: bool = False  # smpsNoAttack flag


@dataclass
class SmpsEffect:
    effect_type: str      # e.g. "smpsSetvoice", "smpsModSet", etc.
    params: list = field(default_factory=list)


@dataclass
class SmpsEvent:
    """Union of note or effect event."""
    note: SmpsNote | None = None
    effect: SmpsEffect | None = None
    tick_position: int = 0  # Cumulative tick position in the channel

    @property
    def is_note(self):
        return self.note is not None

    @property
    def is_effect(self):
        return self.effect is not None


@dataclass
class SmpsChannelHeader:
    channel_type: str     # "DAC", "FM", "PSG"
    label: str
    pitch_offset: int = 0
    volume: int = 0
    # PSG-specific
    mod_byte: int = 0
    voice: int = 0
    psg_voice_label: str = ""  # initial smpsPSGvoice label from smpsHeaderPSG (e.g. "fTone_06")


@dataclass
class SmpsSongHeader:
    voice_label: str = ""
    fm_count: int = 0
    psg_count: int = 0
    tempo_divider: int = 1
    tempo_modifier: int = 5
    channels: list = field(default_factory=list)  # list of SmpsChannelHeader


@dataclass
class SmpsChannel:
    header: SmpsChannelHeader
    events: list = field(default_factory=list)  # list of SmpsEvent
    has_jump: bool = False
    jump_target_label: str = ""


@dataclass
class SmpsVoice:
    index: int
    algorithm: int = 0
    feedback: int = 0
    # Store raw params for informational purposes
    params: dict = field(default_factory=dict)


@dataclass
class SmpsSong:
    header: SmpsSongHeader
    channels: list = field(default_factory=list)  # list of SmpsChannel
    voices: list = field(default_factory=list)     # list of SmpsVoice
    label_tick_pos: dict = field(default_factory=dict)  # label_name -> cumulative tick position


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class SmpsParser:
    def __init__(self):
        self.lines = []
        self.labels = {}           # label_name -> line_index
        self.label_tick_pos = {}   # label_name -> tick position (computed during parsing)

    def parse_file(self, filepath):
        """Parse an SMPS assembly file into a SmpsSong.

        Args:
            filepath: Path to the .asm file

        Returns:
            SmpsSong with header, channels, and voices
        """
        with open(filepath) as f:
            text = f.read()
        return self.parse_text(text)

    def parse_text(self, text):
        """Parse SMPS assembly text into a SmpsSong."""
        self.lines = self._preprocess(text)
        self._collect_labels()

        header = self._parse_header()
        channels = []

        for ch_header in header.channels:
            channel = self._parse_channel_data(ch_header)
            channels.append(channel)

        voices = self._parse_voices(header.voice_label)

        song = SmpsSong(header=header, channels=channels, voices=voices)
        song.label_tick_pos = dict(self.label_tick_pos)
        return song

    def _preprocess(self, text):
        """Strip comments, blank lines, normalize whitespace."""
        result = []
        for raw in text.split('\n'):
            # Strip ; comments
            line = raw[:raw.index(';')] if ';' in raw else raw
            line = line.strip()
            if line:
                result.append(line)
        return result

    def _collect_labels(self):
        """First pass: map label names to line indices."""
        self.labels = {}
        for i, line in enumerate(self.lines):
            if line.endswith(':'):
                label = line[:-1].strip()
                self.labels[label] = i

    def _parse_header(self):
        """Extract song header macros."""
        header = SmpsSongHeader()

        for line in self.lines:
            # smpsHeaderVoice
            m = re.match(r'smpsHeaderVoice\s+(\S+)', line)
            if m:
                header.voice_label = m.group(1)
                continue

            # smpsHeaderChan
            m = re.match(r'smpsHeaderChan\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)', line)
            if m:
                header.fm_count = int(m.group(1), 16)
                header.psg_count = int(m.group(2), 16)
                continue

            # smpsHeaderTempo
            m = re.match(r'smpsHeaderTempo\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)', line)
            if m:
                header.tempo_divider = int(m.group(1), 16)
                header.tempo_modifier = int(m.group(2), 16)
                continue

            # smpsHeaderDAC
            m = re.match(r'smpsHeaderDAC\s+(\S+)', line)
            if m:
                ch = SmpsChannelHeader(
                    channel_type="DAC",
                    label=m.group(1).rstrip(',')
                )
                header.channels.append(ch)
                continue

            # smpsHeaderFM
            m = re.match(r'smpsHeaderFM\s+(\S+?)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)', line)
            if m:
                pitch_raw = int(m.group(2), 16)
                # Pitch offset is signed byte
                if pitch_raw > 0x7F:
                    pitch_raw -= 0x100
                ch = SmpsChannelHeader(
                    channel_type="FM",
                    label=m.group(1).rstrip(','),
                    pitch_offset=pitch_raw,
                    volume=int(m.group(3), 16)
                )
                header.channels.append(ch)
                continue

            # smpsHeaderPSG
            m = re.match(
                r'smpsHeaderPSG\s+(\S+?)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*(\S+)',
                line
            )
            if m:
                pitch_raw = int(m.group(2), 16)
                if pitch_raw > 0x7F:
                    pitch_raw -= 0x100
                ch = SmpsChannelHeader(
                    channel_type="PSG",
                    label=m.group(1).rstrip(','),
                    pitch_offset=pitch_raw,
                    volume=int(m.group(3), 16),
                    mod_byte=int(m.group(4), 16),
                    voice=0,
                    psg_voice_label=m.group(5).rstrip(','),
                )
                header.channels.append(ch)
                continue

        return header

    def _finalize_pending(self, channel, pending_note, tick, last_duration):
        """Emit a pending note with last_duration and advance tick."""
        if pending_note is not None:
            pending_note.duration = last_duration
            channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
            tick += pending_note.duration
        return tick

    def _parse_channel_data(self, ch_header):
        """Parse channel data starting from the channel's label.

        Continues past label boundaries until smpsStop or smpsJump is hit.
        Handles smpsLoop unrolling, smpsCall/smpsReturn inlining.
        """
        channel = SmpsChannel(header=ch_header)
        start_label = ch_header.label

        if start_label not in self.labels:
            print(f"Warning: Label '{start_label}' not found")
            return channel

        start_line = self.labels[start_label] + 1  # Skip the label line itself
        tick = 0
        last_duration = 0
        no_attack_pending = False

        # Track label tick positions
        self.label_tick_pos[start_label] = 0

        is_psg = ch_header.channel_type == "PSG"
        tick, _, _, _ = self._parse_channel_lines(
            channel, start_line, tick, last_duration, no_attack_pending,
            ch_header.channel_type == "DAC", is_psg=is_psg
        )

        return channel

    def _parse_channel_lines(self, channel, start_line, tick, last_duration,
                              no_attack_pending, is_dac, stop_line=None,
                              pending_note=None, last_note_value=0, is_psg=False):
        """Parse lines from start_line, appending events to channel.

        Args:
            stop_line: If set, stop parsing before this line index (used for loop body replay)
            pending_note: Note waiting for a duration from the previous dc.b line.
                          A duration byte in SMPS always applies to the last defined note
                          value, regardless of dc.b line boundaries in the assembly source.
            last_note_value: note_value of the last non-rest, non-DAC note emitted; used
                             so standalone duration bytes retrigger the correct note.

        Returns:
            (tick, last_duration, pending_note, last_note_value) after parsing
        """
        i = start_line
        while i < len(self.lines):
            # Stop before stop_line if set (used by loop unrolling)
            if stop_line is not None and i >= stop_line:
                return tick, last_duration, pending_note, last_note_value

            line = self.lines[i]

            # Check if this line is a label — record its tick position.
            # Labels do not advance the tick, so pending_note is unchanged.
            if line.endswith(':'):
                label_name = line[:-1].strip()
                self.label_tick_pos[label_name] = tick
                i += 1
                continue

            # smpsStop — end of channel.
            # Finalize any pending note before stopping.
            if line.startswith('smpsStop'):
                tick = self._finalize_pending(channel, pending_note, tick, last_duration)
                return tick, last_duration, None, last_note_value

            # smpsJump — song loop point.
            # Finalize any pending note before the jump.
            m = re.match(r'smpsJump\s+(\S+)', line)
            if m:
                tick = self._finalize_pending(channel, pending_note, tick, last_duration)
                channel.has_jump = True
                channel.jump_target_label = m.group(1)
                return tick, last_duration, None, last_note_value

            # smpsLoop — unroll
            m = re.match(r'smpsLoop\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*(\S+)', line)
            if m:
                loop_count = int(m.group(2), 16)
                loop_target = m.group(3)

                # A note pending at the smpsLoop boundary uses SavedDuration in the driver
                # (the loop coord flag is treated as a non-duration byte, so the driver
                # reuses the last saved duration).  Finalize it before replaying.
                if pending_note is not None and not pending_note.is_rest and not pending_note.is_dac:
                    last_note_value = pending_note.note_value
                tick = self._finalize_pending(channel, pending_note, tick, last_duration)
                pending_note = None

                if loop_target in self.labels:
                    target_line = self.labels[loop_target] + 1
                    # The first play-through already happened (lines from target to here).
                    # Replay loop_count - 1 more times, stopping at this smpsLoop line.
                    for _ in range(loop_count - 1):
                        tick, last_duration, loop_pend, last_note_value = self._parse_channel_lines(
                            channel, target_line, tick, last_duration,
                            no_attack_pending, is_dac, stop_line=i,
                            pending_note=None, last_note_value=last_note_value,
                            is_psg=is_psg
                        )
                        # Finalize any note pending at the loop-body end before the next replay
                        if loop_pend is not None and not loop_pend.is_rest and not loop_pend.is_dac:
                            last_note_value = loop_pend.note_value
                        tick = self._finalize_pending(channel, loop_pend, tick, last_duration)
                i += 1
                continue

            # smpsCall — inline subroutine
            m = re.match(r'smpsCall\s+(\S+)', line)
            if m:
                call_target = m.group(1)
                if call_target in self.labels:
                    target_line = self.labels[call_target] + 1
                    tick, last_duration, pending_note, last_note_value = self._parse_call(
                        channel, target_line, tick, last_duration,
                        no_attack_pending, is_dac, pending_note,
                        last_note_value=last_note_value, is_psg=is_psg
                    )
                i += 1
                continue

            # smpsReturn — only hit during call inlining
            if line.startswith('smpsReturn'):
                return tick, last_duration, pending_note, last_note_value

            # Effect macros — do not advance tick; pending_note is unchanged.
            effect = self._try_parse_effect(line)
            if effect is not None:
                channel.events.append(SmpsEvent(effect=effect, tick_position=tick))
                i += 1
                continue

            # dc.b lines — note/duration data.
            # pending_note is carried in and out: a duration byte on the next dc.b line
            # applies to a note that appeared at the end of the previous dc.b line.
            if line.startswith('dc.b'):
                tick, last_duration, no_attack_pending, pending_note, last_note_value = \
                    self._parse_dcb_line(
                        channel, line, tick, last_duration, no_attack_pending, is_dac,
                        pending_note, last_note_value=last_note_value, is_psg=is_psg
                    )
                i += 1
                continue

            i += 1

        # End of file — finalize any remaining pending note
        tick = self._finalize_pending(channel, pending_note, tick, last_duration)
        return tick, last_duration, None, last_note_value

    def _parse_call(self, channel, target_line, tick, last_duration,
                     no_attack_pending, is_dac, pending_note=None, last_note_value=0,
                     is_psg=False):
        """Inline a smpsCall subroutine until smpsReturn."""
        i = target_line
        while i < len(self.lines):
            line = self.lines[i]

            if line.endswith(':'):
                label_name = line[:-1].strip()
                self.label_tick_pos[label_name] = tick
                i += 1
                continue

            if line.startswith('smpsReturn'):
                return tick, last_duration, pending_note, last_note_value

            if line.startswith('smpsStop'):
                tick = self._finalize_pending(channel, pending_note, tick, last_duration)
                return tick, last_duration, None, last_note_value

            effect = self._try_parse_effect(line)
            if effect is not None:
                channel.events.append(SmpsEvent(effect=effect, tick_position=tick))
                i += 1
                continue

            if line.startswith('dc.b'):
                tick, last_duration, no_attack_pending, pending_note, last_note_value = \
                    self._parse_dcb_line(
                        channel, line, tick, last_duration, no_attack_pending, is_dac,
                        pending_note, last_note_value=last_note_value, is_psg=is_psg
                    )
                i += 1
                continue

            i += 1

        return tick, last_duration, pending_note, last_note_value

    def _try_parse_effect(self, line):
        """Try to parse a line as an SMPS effect macro. Returns SmpsEffect or None."""

        # smpsSetvoice / smpsFMvoice
        m = re.match(r'(?:smpsSetvoice|smpsFMvoice)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect('smpsSetvoice', [int(m.group(1), 16)])

        # smpsAlterVol / smpsPSGAlterVol
        m = re.match(r'(?:smpsAlterVol|smpsPSGAlterVol)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            val = int(m.group(1), 16)
            if val > 0x7F:
                val -= 0x100  # signed
            return SmpsEffect('smpsAlterVol', [val])

        # smpsAlterNote / smpsDetune
        m = re.match(r'(?:smpsAlterNote|smpsDetune)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            val = int(m.group(1), 16)
            if val > 0x7F:
                val -= 0x100
            return SmpsEffect('smpsAlterNote', [val])

        # smpsModSet wait,speed,change,step
        m = re.match(
            r'smpsModSet\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)',
            line
        )
        if m:
            return SmpsEffect('smpsModSet', [
                int(m.group(1), 16),
                int(m.group(2), 16),
                int(m.group(3), 16),
                int(m.group(4), 16),
            ])

        # smpsModOn
        if re.match(r'smpsModOn', line):
            return SmpsEffect('smpsModOn', [])

        # smpsModOff
        if re.match(r'smpsModOff', line):
            return SmpsEffect('smpsModOff', [])

        # smpsNoteFill
        m = re.match(r'smpsNoteFill\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect('smpsNoteFill', [int(m.group(1), 16)])

        # smpsPan
        m = re.match(r'smpsPan\s+(.+)', line)
        if m:
            return SmpsEffect('smpsPan', [m.group(1).strip()])

        # smpsNop
        m = re.match(r'smpsNop\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect('smpsNop', [int(m.group(1), 16)])

        # smpsPSGform
        m = re.match(r'smpsPSGform\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect('smpsPSGform', [int(m.group(1), 16)])

        # smpsPSGvoice
        m = re.match(r'smpsPSGvoice\s+(.+)', line)
        if m:
            return SmpsEffect('smpsPSGvoice', [m.group(1).strip()])

        # smpsChangeTransposition / smpsAlterPitch
        m = re.match(r'(?:smpsChangeTransposition|smpsAlterPitch)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            val = int(m.group(1), 16)
            if val > 0x7F:
                val -= 0x100
            return SmpsEffect('smpsChangeTransposition', [val])

        return None

    def _parse_dcb_line(self, channel, line, tick, last_duration, no_attack_pending, is_dac,
                         pending_note=None, last_note_value=0, is_psg=False):
        """Parse a dc.b line containing note/duration/effect data.

        Tokens are comma-separated. Each token is a note name, DAC name, hex value,
        or smpsNoAttack.

        Rules:
        - Note/DAC/rest name → create note event, await optional duration
        - Hex < $80 following a note → duration for that note
        - Hex < $80 not following a note → updates persistent duration
        - smpsNoAttack ($E7) → next note gets is_no_attack=True
        - Hex >= $80 that's not a known note → treat as data/ignored

        pending_note is passed in from the previous dc.b line (may be None).
        A duration byte always applies to the last defined note value, even if
        it appears on the next dc.b assembly line.  The caller is responsible for
        finalizing any pending_note that remains after the last dc.b in a channel.
        """
        # Strip "dc.b" prefix and split on comma
        data_part = line[4:].strip()
        tokens = [t.strip() for t in data_part.split(',')]

        # pending_note comes in from the caller (carries across dc.b lines)

        for token in tokens:
            if not token:
                continue

            # Check for smpsNoAttack
            if token == 'smpsNoAttack':
                no_attack_pending = True
                continue

            # Check for note names
            if token in SMPS_NOTE_NAMES:
                # Finalize any pending note with last_duration
                if pending_note is not None:
                    pending_note.duration = last_duration
                    if not pending_note.is_rest and not pending_note.is_dac:
                        last_note_value = pending_note.note_value
                    channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
                    tick += pending_note.duration

                note_val = SMPS_NOTE_NAMES[token]
                pending_note = SmpsNote(
                    note_value=note_val,
                    duration=0,
                    is_rest=(token == 'nRst'),
                    is_no_attack=no_attack_pending
                )
                no_attack_pending = False
                continue

            # Check for DAC names
            if token in SMPS_DAC_NAMES:
                # Finalize pending note
                if pending_note is not None:
                    pending_note.duration = last_duration
                    if not pending_note.is_rest and not pending_note.is_dac:
                        last_note_value = pending_note.note_value
                    channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
                    tick += pending_note.duration

                dac_val = SMPS_DAC_NAMES[token]
                pending_note = SmpsNote(
                    note_value=dac_val,
                    duration=0,
                    is_dac=True,
                    dac_name=token,
                    is_no_attack=no_attack_pending
                )
                no_attack_pending = False
                continue

            # Check for hex values
            m = re.match(r'\$([0-9A-Fa-f]+)', token)
            if m:
                val = int(m.group(1), 16)

                if val == 0xE7:
                    # smpsNoAttack inline
                    no_attack_pending = True
                    continue

                if val < 0x80:
                    # It's a duration value
                    if pending_note is not None:
                        # Assign to pending note
                        if not pending_note.is_rest and not pending_note.is_dac:
                            last_note_value = pending_note.note_value
                        pending_note.duration = val
                        last_duration = val
                        channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
                        tick += pending_note.duration
                        pending_note = None
                    else:
                        # Standalone duration.
                        # PSG: driver re-triggers (PSGDoNoteOn on each DurationTimeout expiry).
                        # FM/DAC: note sustains naturally — treat as rest/continuation.
                        last_duration = val
                        if is_psg and last_note_value != 0:
                            cont_note = SmpsNote(
                                note_value=last_note_value,
                                duration=val,
                            )
                        else:
                            cont_note = SmpsNote(
                                note_value=0x80,
                                duration=val,
                                is_rest=True,
                                is_no_attack=True,
                            )
                        channel.events.append(SmpsEvent(note=cont_note, tick_position=tick))
                        tick += val
                    continue

                if val >= 0x80:
                    # Could be a note value (nRst=$80, nC0=$81, etc.)
                    # Finalize pending note first
                    if pending_note is not None:
                        pending_note.duration = last_duration
                        if not pending_note.is_rest and not pending_note.is_dac:
                            last_note_value = pending_note.note_value
                        channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
                        tick += pending_note.duration

                    if val == 0x80:
                        pending_note = SmpsNote(
                            note_value=val, duration=0, is_rest=True,
                            is_no_attack=no_attack_pending
                        )
                    elif 0x81 <= val <= 0xDF:
                        # Check if it's a DAC value when in DAC channel
                        if is_dac and val in SMPS_DAC_NAMES.values():
                            dac_name = next(k for k, v in SMPS_DAC_NAMES.items() if v == val)
                            pending_note = SmpsNote(
                                note_value=val, duration=0, is_dac=True,
                                dac_name=dac_name, is_no_attack=no_attack_pending
                            )
                        else:
                            pending_note = SmpsNote(
                                note_value=val, duration=0,
                                is_no_attack=no_attack_pending
                            )
                    else:
                        pending_note = None  # Unknown high byte, skip
                    no_attack_pending = False
                    continue

        # Do NOT finalize pending_note here — return it so the caller can carry it
        # across to the next dc.b line.  A duration byte on the next dc.b line
        # applies to this pending note (SMPS: duration always applies to the last
        # defined note value, regardless of assembly dc.b line boundaries).
        return tick, last_duration, no_attack_pending, pending_note, last_note_value

    def _parse_voices(self, voice_label):
        """Parse voice definitions from smpsVc* macros."""
        voices = []
        if voice_label not in self.labels:
            return voices

        start_line = self.labels[voice_label] + 1
        current_voice = None
        voice_index = 0

        i = start_line
        while i < len(self.lines):
            line = self.lines[i]

            # Stop at next non-voice section or end
            if line.endswith(':') and 'Voice' not in line:
                break

            m = re.match(r'smpsVcAlgorithm\s+\$([0-9A-Fa-f]+)', line)
            if m:
                if current_voice is not None:
                    voices.append(current_voice)
                current_voice = SmpsVoice(index=voice_index)
                current_voice.algorithm = int(m.group(1), 16)
                voice_index += 1
                i += 1
                continue

            if current_voice is not None:
                m = re.match(r'smpsVcFeedback\s+\$([0-9A-Fa-f]+)', line)
                if m:
                    current_voice.feedback = int(m.group(1), 16)

                # Collect other voice params generically
                m = re.match(r'(smpsVc\w+)\s+(.+)', line)
                if m:
                    param_name = m.group(1)
                    param_vals = m.group(2).strip()
                    current_voice.params[param_name] = param_vals

            i += 1

        if current_voice is not None:
            voices.append(current_voice)

        return voices
