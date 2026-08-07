"""SMPS assembly music parser.

Parses Sonic 1 SMPS assembly files (macro-based note data) into an
intermediate representation suitable for conversion to MOD format.
"""

import re
from dataclasses import dataclass, field
from typing import ClassVar

from .tables import SFX_CHANNEL_IDS, SMPS_DAC_NAMES, SMPS_DAC_NAMES_REVERSE, SMPS_NOTE_NAMES

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
    # True when synthesised from a standalone duration byte rather than an explicit note
    # byte.  The driver's .gotduration path skips FMSetFreq/PSGSetFreq entirely, so the
    # channel re-keys at its EXISTING frequency — which differs from re-deriving it if a
    # smpsChangeTransposition landed in between (SndA8 - SS Goal does exactly that).
    is_retrigger: bool = False


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
    # SFX-specific: raw chanid byte from smpsHeaderSFXChannel (cFM5 = $05, cPSG3 = $C0, ...).
    # Music headers imply the hardware channel by declaration order; SFX headers do not, so
    # cFM3/cFM4/cFM5 are indistinguishable without this.  0 = not an SFX channel.
    hw_channel: int = 0


@dataclass
class SmpsSongHeader:
    voice_label: str = ""
    fm_count: int = 0
    psg_count: int = 0
    tempo_divider: int = 1
    tempo_modifier: int = 5
    channels: list = field(default_factory=list)  # list of SmpsChannelHeader
    # True when parsed from smpsHeader*SFX* macros.  SFX have no tempo modifier byte and run
    # one tick per V-int unconditionally — the music (modifier-1)/modifier rate correction
    # must not be applied to them.
    is_sfx: bool = False


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
            channel = self._parse_channel_data(ch_header, tempo_divider=header.tempo_divider)
            channels.append(channel)

        voices = self._parse_voices(header.voice_label)

        song = SmpsSong(header=header, channels=channels, voices=voices)
        song.label_tick_pos = dict(self.label_tick_pos)
        return song

    _CONDITIONAL_DEFAULTS: ClassVar[dict[str, bool]] = {
        "FixMusicAndSFXDataBugs": True,
    }

    def _preprocess(self, text):
        """Strip comments, blank lines, normalize whitespace."""
        result = []
        stack: list[bool] = []   # each entry = "include lines in this block"

        for raw in text.split('\n'):
            line = raw.strip()

            m_if = re.match(r'\s*if\s+(\w+)\s*$', line)
            m_else = re.match(r'\s*else\s*$', line)
            m_endif = re.match(r'\s*endif\s*$', line)

            if m_if:
                stack.append(self._CONDITIONAL_DEFAULTS.get(m_if.group(1), False))
                continue
            if m_else:
                if stack:
                    stack[-1] = not stack[-1]
                continue
            if m_endif:
                if stack:
                    stack.pop()
                continue

            if stack and not stack[-1]:
                continue

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
        """Extract song header macros.

        Handles both music headers (smpsHeaderChan/Tempo/DAC/FM/PSG) and SFX headers
        (smpsHeaderChanSFX/TempoSFX/SFXChannel).  The two sets cannot collide: the music
        regexes all require whitespace directly after the macro name, which fails against
        the 'S' of 'SFX'.
        """
        header = SmpsSongHeader()

        for line in self.lines:
            # smpsHeaderVoice
            m = re.match(r'smpsHeaderVoice\s+(\S+)', line)
            if m:
                header.voice_label = m.group(1)
                continue

            # smpsHeaderTempoSFX <div> — SFX have no tempo modifier byte at all.
            m = re.match(r'smpsHeaderTempoSFX\s+\$([0-9A-Fa-f]+)', line)
            if m:
                header.tempo_divider = int(m.group(1), 16)
                header.tempo_modifier = 0
                header.is_sfx = True
                continue

            # smpsHeaderChanSFX <count> — single total, not separate FM/PSG counts.
            m = re.match(r'smpsHeaderChanSFX\s+\$([0-9A-Fa-f]+)', line)
            if m:
                header.is_sfx = True
                continue

            # smpsHeaderSFXChannel <chanid>, <label>, <pitch>, <vol>
            # chanid is a symbolic EQU (cFM5, cPSG3, ...), not a hex literal.
            m = re.match(
                r'smpsHeaderSFXChannel\s+(\w+)\s*,\s*(\S+?)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)',
                line
            )
            if m:
                chan_name = m.group(1)
                if chan_name not in SFX_CHANNEL_IDS:
                    print(f"Warning: unknown SFX channel id '{chan_name}' — skipping")
                    continue
                chanid = SFX_CHANNEL_IDS[chan_name]
                pitch_raw = int(m.group(3), 16)
                if pitch_raw > 0x7F:
                    pitch_raw -= 0x100
                ch = SmpsChannelHeader(
                    channel_type="PSG" if chanid & 0x80 else "FM",
                    label=m.group(2).rstrip(','),
                    pitch_offset=pitch_raw,
                    volume=int(m.group(4), 16),
                    hw_channel=chanid,
                )
                header.channels.append(ch)
                header.is_sfx = True
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

    def _label_precedes_duration(self, idx):
        """True if the next data-bearing line is a dc.b whose first token is a duration byte.

        Used to decide whether a label sits between a note byte and its duration byte.
        Skips over consecutive label lines, since those emit no bytes either.
        """
        i = idx
        while i < len(self.lines):
            line = self.lines[i]
            if line.endswith(':'):
                i += 1
                continue
            if not line.startswith('dc.b'):
                return False
            first = line[4:].split(',')[0].strip()
            if not first.startswith('$'):
                return False
            try:
                return int(first[1:], 16) < 0x80
            except ValueError:
                return False
        return False

    def _finalize_pending(self, channel, pending_note, tick, last_duration, last_note_value=0):
        """Emit a pending note with last_duration, advance tick, and return updated state.

        Returns:
            (tick, last_note_value) — last_note_value updated if note was non-rest/non-DAC.
        """
        if pending_note is not None:
            pending_note.duration = last_duration
            if not pending_note.is_rest and not pending_note.is_dac:
                last_note_value = pending_note.note_value
            channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
            tick += pending_note.duration
        return tick, last_note_value

    def _parse_channel_data(self, ch_header, tempo_divider=1):
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
        _seen_labels: set[str] = {start_label}
        tick, _, _, _, _ = self._parse_channel_lines(
            channel, start_line, tick, last_duration, no_attack_pending,
            ch_header.channel_type == "DAC", is_psg=is_psg,
            _seen_labels=_seen_labels, chan_tempo_div=tempo_divider,
        )

        return channel

    def _parse_channel_lines(self, channel, start_line, tick, last_duration,
                              no_attack_pending, is_dac, stop_line=None,
                              pending_note=None, last_note_value=0, is_psg=False,
                              _seen_labels=None, chan_tempo_div=1):
        """Parse lines from start_line, appending events to channel.

        Args:
            stop_line: If set, stop parsing before this line index (used for loop body replay)
            pending_note: Note waiting for a duration from the previous dc.b line.
                          A duration byte in SMPS always applies to the last defined note
                          value, regardless of dc.b line boundaries in the assembly source.
            last_note_value: note_value of the last non-rest, non-DAC note emitted; used
                             so standalone duration bytes retrigger the correct note.
            chan_tempo_div: Per-channel tempo divider (from smpsChanTempoDiv); raw durations
                            are multiplied by this at parse time so tick positions are
                            comparable across channels with different dividers.

        Returns:
            (tick, last_duration, pending_note, last_note_value, chan_tempo_div) after parsing
        """
        if _seen_labels is None:
            _seen_labels = set()
        i = start_line
        while i < len(self.lines):
            # Stop before stop_line if set (used by loop unrolling)
            if stop_line is not None and i >= stop_line:
                return tick, last_duration, pending_note, last_note_value, chan_tempo_div

            line = self.lines[i]

            # Check if this line is a label — record its tick position.
            # If a dc.b line ended with a bare note name (no explicit duration), that note
            # is still pending and hasn't advanced the tick yet.  Finalize it now so the
            # label records the tick *after* the note completes, matching the binary layout
            # where labels always appear at a fresh command boundary.
            if line.endswith(':'):
                label_name = line[:-1].strip()
                # Exception: a label emits no bytes, so if the very next data byte is a
                # duration it still belongs to the pending note.  Finalizing here would
                # wrongly give that note `last_duration` instead.  Carry it across.
                #   SndA3 - Death:   dc.b nB3, $07, smpsNoAttack, nAb3 / label / dc.b $01
                # Corpus scan: this triggers for 2 SFX files and 0 music files, so the
                # music conversion path is bit-identical.
                if pending_note is not None and self._label_precedes_duration(i + 1):
                    self.label_tick_pos[label_name] = tick
                    _seen_labels.add(label_name)
                    i += 1
                    continue
                tick, last_note_value = self._finalize_pending(
                    channel, pending_note, tick, last_duration, last_note_value
                )
                pending_note = None
                self.label_tick_pos[label_name] = tick
                _seen_labels.add(label_name)
                i += 1
                continue

            # smpsStop — end of channel.
            # Finalize any pending note before stopping.
            if line.startswith('smpsStop'):
                tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)
                return tick, last_duration, None, last_note_value, chan_tempo_div

            # smpsJump — loop-back or forward dispatch.
            # Finalize any pending note before the jump.
            m = re.match(r'smpsJump\s+(\S+)', line)
            if m:
                target = m.group(1)
                tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)
                if target in _seen_labels or target not in self.labels:
                    # Loop-back to an already-visited label (backward loop), or unknown
                    # target — record the loop and stop parsing.
                    channel.has_jump = True
                    channel.jump_target_label = target
                    return tick, last_duration, None, last_note_value, chan_tempo_div
                # Unseen target — forward/dispatch jump; follow it without marking as a loop.
                _seen_labels.add(target)
                jump_line = self.labels[target] + 1
                return self._parse_channel_lines(
                    channel, jump_line, tick, last_duration,
                    no_attack_pending, is_dac, stop_line=None,
                    pending_note=None, last_note_value=last_note_value,
                    is_psg=is_psg, _seen_labels=_seen_labels,
                    chan_tempo_div=chan_tempo_div,
                )

            # smpsLoop — unroll
            m = re.match(r'smpsLoop\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*(\S+)', line)
            if m:
                loop_count = int(m.group(2), 16)
                loop_target = m.group(3)

                # A note pending at the smpsLoop boundary uses SavedDuration in the driver
                # (the loop coord flag is treated as a non-duration byte, so the driver
                # reuses the last saved duration).  Finalize it before replaying.
                tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)
                pending_note = None

                if loop_target in self.labels:
                    target_line = self.labels[loop_target] + 1
                    # The first play-through already happened (lines from target to here).
                    # Replay loop_count - 1 more times, stopping at this smpsLoop line.
                    for _ in range(loop_count - 1):
                        tick, last_duration, loop_pend, last_note_value, chan_tempo_div = self._parse_channel_lines(
                            channel, target_line, tick, last_duration,
                            no_attack_pending, is_dac, stop_line=i,
                            pending_note=None, last_note_value=last_note_value,
                            is_psg=is_psg, _seen_labels=_seen_labels,
                            chan_tempo_div=chan_tempo_div,
                        )
                        # Finalize any note pending at the loop-body end before the next replay
                        tick, last_note_value = self._finalize_pending(channel, loop_pend, tick, last_duration, last_note_value)
                i += 1
                continue

            # smpsCall — inline subroutine
            m = re.match(r'smpsCall\s+(\S+)', line)
            if m:
                call_target = m.group(1)
                if call_target in self.labels:
                    target_line = self.labels[call_target] + 1
                    tick, last_duration, pending_note, last_note_value, chan_tempo_div = self._parse_call(
                        channel, target_line, tick, last_duration,
                        no_attack_pending, is_dac, pending_note,
                        last_note_value=last_note_value, is_psg=is_psg,
                        chan_tempo_div=chan_tempo_div,
                    )
                i += 1
                continue

            # smpsReturn — only hit during call inlining
            if line.startswith('smpsReturn'):
                return tick, last_duration, pending_note, last_note_value, chan_tempo_div

            # Effect macros — do not advance tick; pending_note is unchanged.
            effect = self._try_parse_effect(line)
            if effect is not None:
                if effect.effect_type == 'smpsChanTempoDiv':
                    # Parser-time state: update divider, do NOT emit to events.
                    chan_tempo_div = effect.params[0]
                else:
                    # Scale time-valued params so they are in DurationTimeout units,
                    # consistent with the scaled tick positions stored in events.
                    effect = self._scale_effect_params(effect, chan_tempo_div)
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
                        pending_note, last_note_value=last_note_value, is_psg=is_psg,
                        chan_tempo_div=chan_tempo_div,
                    )
                i += 1
                continue

            i += 1

        # End of file — finalize any remaining pending note
        tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)
        return tick, last_duration, None, last_note_value, chan_tempo_div

    def _scale_effect_params(self, effect: 'SmpsEffect', chan_tempo_div: int) -> 'SmpsEffect':
        """Return a copy of effect with time-valued params scaled by chan_tempo_div.

        Duration-valued params must be in the same tick units as stored tick_position
        values (i.e., DurationTimeout = raw_byte * chan_tempo_div) so that the converter
        can use _effective_tpr consistently for all time conversions.

        Scaled params:
          smpsNoteFill params[0] — fill duration: NoteTimeout is decremented every raw
                                   VBlank. The parser stores all durations as raw_ticks ×
                                   chan_tempo_div (VBlank units), so fill_raw is already in
                                   the same units — no conversion needed.
          smpsModSet   params[0] — wait before vibrato (raw ticks → DT units)
          smpsModSet   params[1] — speed_raw; scales _smps_cycle to DT units so that
                                   vibrato_speed = round(16 * effective_tpr / smps_cycle)
                                   gives the same result as the old formula.
        """
        if chan_tempo_div == 1:
            return effect  # no scaling needed
        if effect.effect_type == 'smpsNoteFill':
            # NoteTimeout is decremented every raw VBlank (not per driver tick).
            # The parser stores all durations in VBlank units (raw × chan_tempo_div),
            # so fill_raw is already in the same units — pass it through unchanged.
            return effect
        if effect.effect_type == 'smpsModSet':
            p = list(effect.params)
            p[0] = p[0] * chan_tempo_div   # wait
            p[1] = p[1] * chan_tempo_div   # speed_raw (scales _smps_cycle to DT units)
            return SmpsEffect('smpsModSet', p)
        return effect

    def _parse_call(self, channel, target_line, tick, last_duration,
                     no_attack_pending, is_dac, pending_note=None, last_note_value=0,
                     is_psg=False, chan_tempo_div=1):
        """Inline a smpsCall subroutine until smpsReturn."""
        return self._parse_channel_lines(
            channel, target_line, tick, last_duration,
            no_attack_pending, is_dac, stop_line=None,
            pending_note=pending_note, last_note_value=last_note_value, is_psg=is_psg,
            chan_tempo_div=chan_tempo_div,
        )

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
        if line.startswith('smpsModOn'):
            return SmpsEffect('smpsModOn', [])

        # smpsModOff
        if line.startswith('smpsModOff'):
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

        # smpsChanTempoDiv
        m = re.match(r'smpsChanTempoDiv\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect('smpsChanTempoDiv', [int(m.group(1), 16)])

        return None

    def _parse_dcb_line(self, channel, line, tick, last_duration, no_attack_pending, is_dac,
                         pending_note=None, last_note_value=0, is_psg=False, chan_tempo_div=1):
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
                tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)

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
                tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)

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
                    # It's a duration value; scale by per-channel tempo divider.
                    scaled = val * chan_tempo_div
                    if pending_note is not None:
                        # Assign to pending note
                        if not pending_note.is_rest and not pending_note.is_dac:
                            last_note_value = pending_note.note_value
                        pending_note.duration = scaled
                        last_duration = scaled
                        channel.events.append(SmpsEvent(note=pending_note, tick_position=tick))
                        tick += pending_note.duration
                        pending_note = None
                    else:
                        # Standalone duration.
                        # PSG: driver re-triggers (PSGDoNoteOn on each DurationTimeout expiry),
                        #      UNLESS smpsNoAttack precedes it — SetPSGVolume then skips volume
                        #      write (bit 4 set), so the envelope continues without restart.
                        # DAC: driver re-triggers SavedDAC on each DurationTimeout expiry.
                        # FM:  behavior depends on smpsNoAttack (see else branch below).
                        last_duration = scaled
                        if is_psg and not no_attack_pending and last_note_value != 0:
                            cont_note = SmpsNote(
                                note_value=last_note_value,
                                duration=scaled,
                                is_retrigger=True,
                            )
                        elif is_psg:
                            cont_note = SmpsNote(
                                note_value=0x80,
                                duration=scaled,
                                is_rest=True,
                                is_no_attack=True,
                            )
                        elif is_dac:
                            # Find the most recent note event. If it's a DAC sample, retrigger it.
                            # If it's a rest (SavedDAC=$80), stay silent — driver skips trigger for rests.
                            last_note_evt = next(
                                (e.note for e in reversed(channel.events) if e.note is not None), None
                            )
                            if last_note_evt is not None and last_note_evt.is_dac:
                                cont_note = SmpsNote(
                                    note_value=last_note_evt.note_value,
                                    duration=scaled,
                                    is_dac=True,
                                    dac_name=last_note_evt.dac_name,
                                )
                            else:
                                cont_note = SmpsNote(
                                    note_value=0x80,
                                    duration=scaled,
                                    is_rest=True,
                                    is_no_attack=True,
                                )
                        else:
                            # FM: behavior depends on whether smpsNoAttack preceded the
                            # standalone duration.
                            # Without smpsNoAttack: FMNoteOn fires → key-on → retrigger
                            #   (e.g. 1-Up: $03,$03,$06,$06 after nE7 → staccato arpeggio).
                            # With smpsNoAttack: FMNoteOn suppressed by no-attack flag →
                            #   envelope sustains, no key-on
                            #   (e.g. GHZ: smpsNoAttack,$3C after nF5 → held note).
                            if not no_attack_pending and last_note_value != 0:
                                cont_note = SmpsNote(
                                    note_value=last_note_value,
                                    duration=scaled,
                                    is_retrigger=True,
                                )
                            else:
                                cont_note = SmpsNote(
                                    note_value=0x80,
                                    duration=scaled,
                                    is_rest=True,
                                    is_no_attack=True,
                                )
                        channel.events.append(SmpsEvent(note=cont_note, tick_position=tick))
                        tick += scaled
                    continue

                else:  # val >= 0x80
                    # Could be a note value (nRst=$80, nC0=$81, etc.)
                    # Finalize pending note first
                    tick, last_note_value = self._finalize_pending(channel, pending_note, tick, last_duration, last_note_value)

                    if val == 0x80:
                        pending_note = SmpsNote(
                            note_value=val, duration=0, is_rest=True,
                            is_no_attack=no_attack_pending
                        )
                    elif 0x81 <= val <= 0xDF:
                        # Check if it's a DAC value when in DAC channel
                        if is_dac and val in SMPS_DAC_NAMES.values():
                            dac_name = SMPS_DAC_NAMES_REVERSE.get(val, '')
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
