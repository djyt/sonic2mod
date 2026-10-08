"""A song's code written back as SMPS2ASM assembly - readable, editable, and read by SmpsParser
into the same song (the round trip checks both).

Labels are named by role, as the disassembly names them; a ROM's addresses follow as comments:

    Mus81_Header:   ; $745DC     header macros: smpsHeaderStartSong ... smpsHeaderPSG
    Mus81_FM1:      ; $7460C     a track's start: DAC, FM1..., PSG1... (an SFX: its channel, FM5)
        smpsCall    Mus81_Call00
    Mus81_Loop00:   ; $74615     a target, named by what reaches it first: Jump / Loop / Call, numbered
        dc.b        nE7, $04     bytes as note / DAC names, durations, smpsNoAttack
    Mus81_Voices:   ; $74C63     the voices: smpsVcAlgorithm ... smpsVcTotalLevel
"""

from __future__ import annotations

from collections import Counter

from .code import NO_ATTACK, REST, Op, OpKind, SongCode
from .driver_tables import PAN_VALUES
from .names import SFX_CHANNEL_IDS, SMPS_DAC_NAMES_REVERSE, flag_name, note_label, voice_macro
from .song import CoordFlag, SmpsChannelHeader, SmpsVoice, VoiceField

_BYTES_PER_LINE = 12
_PAN_SPEAKERS = 0xC0
_PAN_LFO = 0x3F
_ENDS = frozenset({OpKind.STOP, OpKind.RETURN, OpKind.JUMP})
_FOLLOWS = frozenset({OpKind.CALL, OpKind.JUMP, OpKind.LOOP})
_TARGET_ROLES = {OpKind.JUMP: "Jump", OpKind.LOOP: "Loop", OpKind.CALL: "Call"}
_UNREACHED_ROLE = "Label"

_PAN_NAMES = {v: k for k, v in reversed(PAN_VALUES.items())}       # 0xC0: panCentre
_SFX_CHANNEL_NAMES = {v: k for k, v in SFX_CHANNEL_IDS.items()}    # 0x05: cFM5
_CHANNEL_ID_PREFIX = "c"

# The voice macros in SMPS2ASM's order
_VOICE_ORDER = (VoiceField.DETUNE, VoiceField.MULTIPLE, VoiceField.RATE_SCALE, VoiceField.ATTACK_RATE,
                VoiceField.AMP_MOD, VoiceField.DECAY_RATE_1, VoiceField.DECAY_RATE_2, VoiceField.DECAY_LEVEL,
                VoiceField.RELEASE_RATE, VoiceField.TOTAL_LEVEL)

_MACRO_WIDTH = 20       # the operand column
_LABEL_WIDTH = 24       # the address comment's column


def write_asm(song: SongCode, name: str, comment: str = "") -> str:
    """The song as an .asm file; `name` prefixes its labels (Mus81), `comment` heads the file.
    ValueError for what SMPS2ASM has no spelling of: a track's chip channel, a voice's pan byte
    (Type 0 FM's drum track and voices)."""
    unspellable = [what for what, found in (
        ("a track that states its chip channel", any(c.chip_channel for c in song.header.channels)),
        ("a voice that stores its pan", any(v.pan is not None for v in song.voices))) if found]
    if unspellable:
        raise ValueError(f"no SMPS2ASM spelling of {' or '.join(unspellable)} ({song.driver})")
    return _Writer(song, name).text(comment)


def _macro(macro: str, operands: str = "") -> str:
    return f"\t{macro:<{_MACRO_WIDTH - 1}} {operands}".rstrip()


def _hex(value: int) -> str:
    return f"${value & 0xFF:02X}"


class _Writer:
    def __init__(self, song: SongCode, name: str):
        self._song = song
        self._name = name
        self._names = self._label_names()

    def text(self, comment: str) -> str:
        lines = [f"; {line}" for line in comment.splitlines()]
        lines += self._header()
        lines += self._code()
        lines += self._voices()
        return "\n".join(lines) + "\n"

    # --- names ------------------------------------------------------------------------

    def _label_names(self) -> dict[str, str]:
        """Each label's name: a track start by its channel, the voice bank, every other label by
        what reaches it first, numbered per role in code order (Mus81_Loop00, Mus81_Loop01)."""
        h, ops = self._song.header, self._song.code.ops
        names: dict[str, str] = {}
        roles: Counter[str] = Counter()
        for c in h.channels:
            names.setdefault(c.label, f"{self._name}_{self._channel_role(c, roles)}")
        if h.voice_label:
            names[h.voice_label] = f"{self._name}_Voices"

        reached_by: dict[str, str] = {}
        for op in ops:
            if op.kind in _FOLLOWS:
                reached_by.setdefault(op.name, _TARGET_ROLES[op.kind])
        numbers: Counter[str] = Counter()
        for op in ops:
            if op.kind is not OpKind.LABEL or op.name in names:
                continue
            role = reached_by.get(op.name, _UNREACHED_ROLE)
            names[op.name] = f"{self._name}_{role}{numbers[role]:02X}"
            numbers[role] += 1
        return names

    def _channel_role(self, c: SmpsChannelHeader, roles: Counter[str]) -> str:
        """DAC, FM1..., PSG1... in header order; an SFX track by its channel id (cFM5: FM5)."""
        if c.hw_channel:
            return _SFX_CHANNEL_NAMES[c.hw_channel].removeprefix(_CHANNEL_ID_PREFIX)
        if c.channel_type == "DAC":
            return "DAC"
        roles[c.channel_type] += 1
        return f"{c.channel_type}{roles[c.channel_type]}"

    def _name_of(self, label: str) -> str:
        return self._names.get(label, label)

    def _label_line(self, name: str, address: int | None) -> str:
        """`name:`, the ROM address it came from as a comment."""
        line = f"{name}:"
        return line if address is None else f"{line:<{_LABEL_WIDTH}}; ${address:05X}"

    # --- header -----------------------------------------------------------------------

    def _header(self) -> list[str]:
        h = self._song.header
        lines = [self._label_line(f"{self._name}_Header", self._song.address), _macro("smpsHeaderStartSong", "1")]
        lines.append(_macro("smpsHeaderVoice", self._name_of(h.voice_label)) if h.voice_label
                     else _macro("smpsHeaderVoiceNull"))

        if h.is_sfx:
            lines += [_macro("smpsHeaderTempoSFX", _hex(h.tempo_divider)),
                      _macro("smpsHeaderChanSFX", _hex(len(h.channels))), ""]
            lines += [_macro("smpsHeaderSFXChannel", f"{_SFX_CHANNEL_NAMES[c.hw_channel]}, {self._name_of(c.label)}, "
                                                     f"{_hex(c.pitch_offset)}, {_hex(c.volume)}") for c in h.channels]
            return lines

        lines += [_macro("smpsHeaderChan", f"{_hex(h.fm_count)}, {_hex(h.psg_count)}"),
                  _macro("smpsHeaderTempo", f"{_hex(h.tempo_divider)}, {_hex(h.tempo_modifier)}"), ""]
        lines += [self._channel_header(c) for c in h.channels]
        return lines

    def _channel_header(self, c: SmpsChannelHeader) -> str:
        label = self._name_of(c.label)
        if c.channel_type == "DAC":
            return _macro("smpsHeaderDAC", label)
        if c.channel_type == "FM":
            return _macro("smpsHeaderFM", f"{label}, {_hex(c.pitch_offset)}, {_hex(c.volume)}")
        return _macro("smpsHeaderPSG", f"{label}, {_hex(c.pitch_offset)}, {_hex(c.volume)}, "
                                       f"{_hex(c.mod_byte)}, {c.psg_voice_label}")

    # --- code -------------------------------------------------------------------------

    def _code(self) -> list[str]:
        """The ops, dc.b bytes gathered into lines; DAC tracks' bytes named as samples."""
        ops = self._song.code.ops
        dac = set().union(*(self._reached(c.label) for c in self._song.header.channels if c.channel_type == "DAC"))
        lines: list[str] = []
        tokens: list[str] = []

        def flush() -> None:
            for i in range(0, len(tokens), _BYTES_PER_LINE):
                lines.append(_macro("dc.b", ", ".join(tokens[i:i + _BYTES_PER_LINE])))
            tokens.clear()

        for i, op in enumerate(ops):
            if op.kind is OpKind.BYTE:
                tokens.append(_byte(op.value, i in dac))
                continue
            flush()
            if op.kind is OpKind.LABEL:
                lines += ["", self._label_line(self._name_of(op.name), self._song.addresses.get(op.name))]
                continue
            lines.append(self._control(op))
        flush()
        return lines

    def _reached(self, label: str) -> set[int]:
        """The op indices a track's walk can reach from `label`."""
        ops, labels = self._song.code.ops, self._song.code.labels
        reached: set[int] = set()
        todo = [labels[label]] if label in labels else []
        while todo:
            i = todo.pop()
            while i < len(ops) and i not in reached:
                reached.add(i)
                op = ops[i]
                if op.kind in _FOLLOWS and op.name in labels:
                    todo.append(labels[op.name])
                if op.kind in _ENDS:
                    break
                i += 1
        return reached

    def _control(self, op: Op) -> str:
        if op.kind is OpKind.STOP:
            return _macro("smpsStop")
        if op.kind is OpKind.RETURN:
            return _macro("smpsReturn")
        if op.kind is OpKind.JUMP:
            return _macro("smpsJump", self._name_of(op.name))
        if op.kind is OpKind.CALL:
            return _macro("smpsCall", self._name_of(op.name))
        if op.kind is OpKind.LOOP:
            return _macro("smpsLoop", f"{_hex(op.index)}, {_hex(op.value)}, {self._name_of(op.name)}")

        assert op.effect is not None
        flag, params = op.effect.flag, op.effect.params
        if flag == CoordFlag.PAN:
            return _macro(flag_name(flag), f"{_PAN_NAMES[params[0] & _PAN_SPEAKERS]}, {_hex(params[0] & _PAN_LFO)}")
        if flag == CoordFlag.PSG_VOICE:
            return _macro(flag_name(flag), str(params[0]))
        return _macro(flag_name(flag), ", ".join(_hex(p) for p in params))

    # --- voices -----------------------------------------------------------------------

    def _voices(self) -> list[str]:
        song = self._song
        if not song.voices:
            return []
        label = song.header.voice_label
        lines = ["", self._label_line(self._name_of(label), song.addresses.get(label))]
        for voice in song.voices:
            lines += _voice(voice)
        return lines


def _byte(value: int, is_dac: bool) -> str:
    if value == NO_ATTACK:
        return "smpsNoAttack"
    if value < REST:
        return _hex(value)
    if is_dac and value in SMPS_DAC_NAMES_REVERSE:
        return SMPS_DAC_NAMES_REVERSE[value]
    return note_label(value)


def _voice(voice: SmpsVoice) -> list[str]:
    lines = [f";\tVoice {_hex(voice.index)}",
             _macro("smpsVcAlgorithm", _hex(voice.algorithm)),
             _macro("smpsVcFeedback", _hex(voice.feedback)),
             _macro("smpsVcUnusedBits", _hex(0))]
    lines += [_macro(voice_macro(f), ", ".join(_hex(v) for v in voice.operator_values(f))) for f in _VOICE_ORDER]
    return [*lines, ""]
