"""SMPS assembly music parser.

Reads Sonic 1 SMPS assembly (SMPS2ASM macros) into the song's code (core/smps/code.py), which the
driver's reading rules turn into the intermediate representation suitable for conversion to MOD.
"""

import re

from .code import NO_ATTACK, Op, OpKind, SmpsCode, effect_from_bytes, song_from_code, track_byte
from .driver_tables import PAN_VALUES
from .names import SFX_CHANNEL_IDS, SMPS_DAC_NAMES, SMPS_NOTE_NAMES, voice_field_from_macro
from .rules import PlaybackRules
from .song import ChannelType, CoordFlag, SmpsChannelHeader, SmpsEffect, SmpsSongHeader, SmpsVoice

_PAN_LFO_MASK = 0x3F  # smpsPan's second operand: B4's AMS / FMS bits

# Control macros: the op each is, and its operands' pattern
_JUMP = re.compile(r'smpsJump\s+(\S+)')
_LOOP = re.compile(r'smpsLoop\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*(\S+)')
_CALL = re.compile(r'smpsCall\s+(\S+)')
_HEX = re.compile(r'\$([0-9A-Fa-f]+)')

# Assembly conditionals: `if Symbol` / `if Symbol=0`
_IF = re.compile(r'\s*if\s+(\w+)\s*(=\s*0)?\s*$')
_FIX_DATA_BUGS = "FixMusicAndSFXDataBugs"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class SmpsParser:
    def __init__(self, rules: PlaybackRules, fix_data_bugs: bool = True):
        """rules: the rules of the driver the asm is written for (an SMPS2ASM disassembly: Sonic 1's).
        fix_data_bugs: the disassembly's FixMusicAndSFXDataBugs; False reads the songs as the game
        shipped them (the ROM, every VGZ): Marble Zone PSG3's three notes off the PSG table,
        Credits' three late rests and the smpsAlterVol that mutes the passage after them."""
        self.lines = []
        self.labels = {}           # label_name -> line_index
        self._symbols = {_FIX_DATA_BUGS: fix_data_bugs}
        self._rules = rules

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
        voices = self._parse_voices(header.voice_label)
        return song_from_code(header, self._code(), voices, self._rules)

    def _preprocess(self, text):
        """Strip comments, blank lines, normalize whitespace."""
        result = []
        stack: list[bool] = []   # each entry = "include lines in this block"

        for raw in text.split('\n'):
            line = raw.strip()

            m_if = _IF.match(line)
            m_else = re.match(r'\s*else\s*$', line)
            m_endif = re.match(r'\s*endif\s*$', line)

            if m_if:
                # `if Symbol` or `if Symbol=0`; an unknown symbol reads as 0
                value = self._symbols.get(m_if.group(1), False)
                stack.append(not value if m_if.group(2) else value)
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
                    channel_type=ChannelType.PSG if chanid & 0x80 else ChannelType.FM,
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
                    channel_type=ChannelType.DAC,
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
                    channel_type=ChannelType.FM,
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
                    channel_type=ChannelType.PSG,
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

    def _code(self) -> SmpsCode:
        """The lines as ops: labels, control macros, flags, dc.b bytes.  Lines that are none of
        these (header macros, voices, `even`) emit no op."""
        ops: list[Op] = []
        for line in self.lines:
            ops.extend(self._line_ops(line))
        return SmpsCode(ops)

    def _line_ops(self, line: str) -> list[Op]:
        if line.endswith(':'):
            return [Op(OpKind.LABEL, name=line[:-1].strip())]

        # smpsFade (the 1-Up jingle restores the song it interrupted) and smpsStopSpecial (the
        # waterfall SFX hands FM4 back to the music) end the track as smpsStop does
        if line.startswith(('smpsStop', 'smpsFade')):
            return [Op(OpKind.STOP)]

        if line.startswith('smpsReturn'):
            return [Op(OpKind.RETURN)]

        m = _JUMP.match(line)
        if m:
            return [Op(OpKind.JUMP, name=m.group(1))]

        m = _LOOP.match(line)
        if m:
            return [Op(OpKind.LOOP, value=int(m.group(2), 16), name=m.group(3), index=int(m.group(1), 16))]

        m = _CALL.match(line)
        if m:
            return [Op(OpKind.CALL, name=m.group(1))]

        effect = self._try_parse_effect(line)
        if effect is not None:
            return [Op(OpKind.EFFECT, effect=effect)]

        if line.startswith('dc.b'):
            ops = (track_byte(v) for v in map(_dcb_byte, line[4:].split(',')) if v is not None)
            return [op for op in ops if op is not None]
        return []

    def _try_parse_effect(self, line):
        """Try to parse a line as an SMPS effect macro. Returns SmpsEffect or None."""

        # smpsSetvoice / smpsFMvoice
        m = re.match(r'(?:smpsSetvoice|smpsFMvoice)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.SET_VOICE, [int(m.group(1), 16)])

        # smpsAlterVol / smpsPSGAlterVol
        m = re.match(r'(?:smpsAlterVol|smpsPSGAlterVol)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return effect_from_bytes(CoordFlag.ALTER_VOL, [int(m.group(1), 16)])

        # smpsAlterNote / smpsDetune
        m = re.match(r'(?:smpsAlterNote|smpsDetune)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return effect_from_bytes(CoordFlag.DETUNE, [int(m.group(1), 16)])

        # smpsModSet wait,speed,change,step
        m = re.match(
            r'smpsModSet\s+\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)\s*,\s*\$([0-9A-Fa-f]+)',
            line
        )
        if m:
            return SmpsEffect(CoordFlag.MOD_SET, [
                int(m.group(1), 16),
                int(m.group(2), 16),
                int(m.group(3), 16),
                int(m.group(4), 16),
            ])

        # smpsModOn
        if line.startswith('smpsModOn'):
            return SmpsEffect(CoordFlag.MOD_ON, [])

        # smpsModOff
        if line.startswith('smpsModOff'):
            return SmpsEffect(CoordFlag.MOD_OFF, [])

        # smpsNoteFill
        m = re.match(r'smpsNoteFill\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.NOTE_FILL, [int(m.group(1), 16)])

        # smpsPan
        m = re.match(r'smpsPan\s+(.+)', line)
        if m:
            return SmpsEffect(CoordFlag.PAN, [_pan_byte(m.group(1))])

        # smpsNop
        m = re.match(r'smpsNop\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.NOP, [int(m.group(1), 16)])

        # smpsPSGform
        m = re.match(r'smpsPSGform\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.PSG_FORM, [int(m.group(1), 16)])

        # smpsPSGvoice
        m = re.match(r'smpsPSGvoice\s+(.+)', line)
        if m:
            return SmpsEffect(CoordFlag.PSG_VOICE, [m.group(1).strip()])

        # smpsChangeTransposition / smpsAlterPitch
        m = re.match(r'(?:smpsChangeTransposition|smpsAlterPitch)\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return effect_from_bytes(CoordFlag.CHANGE_TRANSPOSITION, [int(m.group(1), 16)])

        # smpsChanTempoDiv
        m = re.match(r'smpsChanTempoDiv\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.CHAN_TEMPO_DIV, [int(m.group(1), 16)])

        # smpsSetTempoMod ($EA, cfSetTempo): new tempo modifier for EVERY track, and the
        # TempoWait counter restarts.  The converter turns it into an Fxx BPM change.
        m = re.match(r'smpsSetTempoMod\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.SET_TEMPO_MOD, [int(m.group(1), 16)])

        # smpsSetTempoDiv ($EB, cfSetTempoDividerAll): every track's duration divider, from the
        # note read after it.  Kept as an event; song_prep.apply_global_tempo_div re-times the
        # channels (Credits only).
        m = re.match(r'smpsSetTempoDiv\s+\$([0-9A-Fa-f]+)', line)
        if m:
            return SmpsEffect(CoordFlag.SET_TEMPO_DIV, [int(m.group(1), 16)])

        return None

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

                # An operator field's macro: its hex bytes ('$00, $05, $00, $05')
                m = re.match(r'(smpsVc\w+)\s+(.+)', line)
                field_ = voice_field_from_macro(m.group(1)) if m else None
                if m and field_ is not None:
                    current_voice.operators[field_] = tuple(
                        int(v.strip().lstrip('$'), 16) for v in m.group(2).split(','))

            i += 1

        if current_voice is not None:
            voices.append(current_voice)

        return voices




def _dcb_byte(token: str) -> int | None:
    """A dc.b token's byte: smpsNoAttack, a note or DAC name, a hex value; None for anything else."""
    token = token.strip()
    if token == 'smpsNoAttack':
        return NO_ATTACK
    if token in SMPS_NOTE_NAMES:
        return SMPS_NOTE_NAMES[token]
    if token in SMPS_DAC_NAMES:
        return SMPS_DAC_NAMES[token]
    m = _HEX.match(token)
    return int(m.group(1), 16) if m else None


def _pan_byte(operands: str) -> int:
    """smpsPan's operands ('panLeft, $00') as the B4 byte the driver writes: direction | AMS/FMS."""
    parts = [p.strip() for p in operands.split(",")]
    if parts[0] not in PAN_VALUES:
        raise ValueError(f"smpsPan: unknown direction {parts[0]!r}")
    lfo = int(parts[1][1:], 16) if len(parts) > 1 and parts[1].startswith("$") else 0
    return (PAN_VALUES[parts[0]] | (lfo & _PAN_LFO_MASK)) & 0xFF
