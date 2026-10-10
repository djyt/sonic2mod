"""SMPS names: note labels (nC0..), real-pitch config names (A4), DAC samples, SFX channel ids, source channels."""

from .effects import CoordFlag
from .song import ChannelType, VoiceField

# ---------------------------------------------------------------------------
# SMPS Note Names → byte values
# Derived from _smps2asm_inc.asm enumeration
# nRst=$80, nC0=$81, 12 semitones per octave
# ---------------------------------------------------------------------------

def _build_smps_note_names():
    """Build the SMPS note name to byte value mapping."""
    names = {}
    names['nRst'] = 0x80

    # Standard chromatic note names per octave
    # Each octave has: C, Cs, D, Ds, E, Es(=F), Fs, G, Gs, A, As, B, Bs(=next C)
    # The enumeration in _smps2asm_inc.asm uses enharmonic aliases
    chromatic = ['C', 'Cs', 'D', 'Ds', 'E', 'Es', 'Fs', 'G', 'Gs', 'A', 'As', 'B']

    for octave in range(8):  # 0-7
        base = 0x81 + (octave * 12)
        for i, name in enumerate(chromatic):
            val = base + i
            if val > 0xFF:
                break
            # Primary name: nC0, nCs0, etc.
            note_name = f'n{name}{octave}'
            names[note_name] = val

        # Enharmonic aliases
        # nDb = nCs, nEb = nDs, nFb = nE, nF = nEs, nGb = nFs, nAb = nGs, nBb = nAs
        # nCb(next) = nB, nBs = next C
        enharmonics = {
            f'nDb{octave}': f'nCs{octave}',
            f'nEb{octave}': f'nDs{octave}',
            f'nFb{octave}': f'nE{octave}',
            f'nF{octave}': f'nEs{octave}',
            f'nGb{octave}': f'nFs{octave}',
            f'nAb{octave}': f'nGs{octave}',
            f'nBb{octave}': f'nAs{octave}',
        }
        for alias, canonical in enharmonics.items():
            if canonical in names:
                names[alias] = names[canonical]

        # nBs{octave} = nC{octave+1}
        bs_name = f'nBs{octave}'
        b_val = base + 11  # B of this octave
        if b_val < 0xFF:
            names[bs_name] = b_val + 1  # = next octave's C
            # nC{octave+1} = nBs{octave} (already set by primary loop for next octave)

        # nCb{octave+1} = nB{octave}
        cb_name = f'nCb{octave + 1}'
        b_name = f'nB{octave}'
        if b_name in names:
            names[cb_name] = names[b_name]

    # nMaxPSG = nA5 for Sonic 1 (SonicDriverVer <= 2)
    names['nMaxPSG'] = names['nA5']  # $C6

    return names


SMPS_NOTE_NAMES = _build_smps_note_names()

# The spelling written for each note byte (the disassembly's: nEb, nF, nAb, nBb)
_LABEL_NAMES = ['C', 'Cs', 'D', 'Eb', 'E', 'F', 'Fs', 'G', 'Ab', 'A', 'Bb', 'B']
_REST_BYTE = 0x80
_FIRST_NOTE = 0x81


def note_label(byte: int) -> str:
    """A note byte as SMPS2ASM spells it: $80 'nRst', $81 'nC0', $AF 'nBb3'."""
    if byte == _REST_BYTE:
        return 'nRst'
    octave, step = divmod(byte - _FIRST_NOTE, len(_LABEL_NAMES))
    return f'n{_LABEL_NAMES[step]}{octave}'

_CHROMATIC_NAMES = ['C', 'Cs', 'D', 'Ds', 'E', 'Es', 'Fs', 'G', 'Gs', 'A', 'As', 'B']

def semitone_to_note_name(semitone: int) -> str:
    """Return SMPS primary note name for a semitone offset from C0 (e.g. 84 → 'C7')."""
    octave = semitone // 12
    note   = semitone % 12
    return f"{_CHROMATIC_NAMES[note]}{octave}"

# The same twelve names as _CHROMATIC_NAMES, except that index 5 is spelled 'F' rather
# than the driver's 'Es'.  This is the spelling YAML configs use (low:, high:, root:,
# synth_root:) and the one parse_synth_note accepts, so it is what anything writing a
# config must emit.  Keeping the two apart matters: 'Es' is an SMPS label, 'F' is not.
_SYNTH_NAMES = ('C', 'Cs', 'D', 'Ds', 'E', 'F', 'Fs', 'G', 'Gs', 'A', 'As', 'B')


def synth_note_name(semitone: int) -> str:
    """Semitone offset from C0 as a YAML config note name (84 -> 'C7').  Inverse of parse_synth_note."""
    return f"{_SYNTH_NAMES[semitone % 12]}{semitone // 12}"


def parse_smps_note(name: str) -> int:
    """Convert an SMPS note name to semitone offset from C0 (0 = C0, 12 = C1, …).

    Accepts the same identifiers used in _smps2asm_inc.asm without the leading
    'n' prefix — e.g. 'G5', 'Cs6', 'Ab6', 'C7'.

    Returns:
        Integer semitone (0–95 for the standard 8-octave SMPS range).

    Raises:
        ValueError: if the name is not recognised.
    """
    key = 'n' + name
    if key not in SMPS_NOTE_NAMES:
        raise ValueError(f"Unknown SMPS note name: '{name}' (tried '{key}')")
    return SMPS_NOTE_NAMES[key] - 0x81


_ENHARMONIC_MAP = {
    'Db': 'Cs', 'Eb': 'Ds', 'Fb': 'E', 'F': 'Es',
    'Gb': 'Fs', 'Ab': 'Gs', 'Bb': 'As',
}


def parse_synth_note(name: str) -> int:
    """Like parse_smps_note but not limited to the SMPS byte range (octaves 0–7).

    Used for synth_root only, where the note is a synthesis frequency target,
    not an SMPS playback event. Supports C8, Fs9, etc.

    Returns:
        Integer semitone offset from C0 (e.g. C8 → 96, C9 → 108).

    Raises:
        ValueError: if the note name is not recognised.
    """
    import re
    m = re.fullmatch(r'([A-G][sb]?)(\d+)', name)
    if not m:
        raise ValueError(f"Invalid synth note name: '{name}'")
    note_str, octave_str = m.group(1), m.group(2)
    canonical = _ENHARMONIC_MAP.get(note_str, note_str)
    if canonical not in _CHROMATIC_NAMES:
        raise ValueError(f"Invalid synth note name: '{name}'")
    return _CHROMATIC_NAMES.index(canonical) + int(octave_str) * 12


# ---------------------------------------------------------------------------
# SMPS2ASM's DAC sample names → byte values (_smps2asm_inc.asm: the asm dialect's, Sonic 1's samples)
# ---------------------------------------------------------------------------

SMPS_DAC_NAMES = {
    'dKick':        0x81,
    'dSnare':       0x82,
    'dTimpani':     0x83,
    'dHiTimpani':   0x88,
    'dMidTimpani':  0x89,
    'dLowTimpani':  0x8A,
    'dVLowTimpani': 0x8B,
}

# Reverse lookup: DAC byte value → name (built once at import time)
SMPS_DAC_NAMES_REVERSE: dict[int, str] = {v: k for k, v in SMPS_DAC_NAMES.items()}


# ---------------------------------------------------------------------------
# SFX channel ids (smpsHeaderSFXChannel chanid) — _smps2asm_inc.asm:171-178
# ---------------------------------------------------------------------------
#
# These are the raw VoiceControl bytes the driver stores per track.  For FM they
# double as the YM2612 $28 key-on/off channel bits; for PSG as the latch channel
# bits.  Music headers imply the hardware channel by declaration order, so this
# mapping only matters for SFX.
#
# cFM6 ($06) is S3/S&K only and is rejected by the macro for Sonic 1.

SFX_CHANNEL_IDS = {
    'cFM3':   0x02,
    'cFM4':   0x04,
    'cFM5':   0x05,
    'cPSG1':  0x80,
    'cPSG2':  0xA0,
    'cPSG3':  0xC0,
    'cNoise': 0xE0,
}


# ---------------------------------------------------------------------------
# Source channel names
# ---------------------------------------------------------------------------


FM_CHANNEL_NAMES = ("FM1", "FM2", "FM3", "FM4", "FM5", "FM6")     # the YM2612's channels, in order
PSG_CHANNEL_NAMES = ("PSG1", "PSG2", "PSG3")                          # the SN76489's tone channels


def source_names(song) -> list[str]:
    """"DAC", "FM1".."FMn", "PSG1".."PSGn" for a parsed song's channels, in header order; a
    channel that states its chip channel (Type 0 FM's FM1 FM2 FM4 FM5 FM6) is named by it."""
    return track_names([ch.header for ch in song.channels])


def track_names(headers: list) -> list[str]:
    """source_names for a header's tracks (SmpsChannelHeader), before they are walked."""
    names: list[str] = []
    fm = psg = 0
    for header in headers:
        kind = header.channel_type
        if header.chip_channel:
            names.append(header.chip_channel)
        elif kind == ChannelType.DAC:
            names.append("DAC")
        elif kind == ChannelType.FM:
            fm += 1
            names.append(f"FM{fm}")
        else:
            psg += 1
            names.append(f"PSG{psg}")
    return names


def source_map(song) -> dict:
    """Source name -> parsed channel, in header order."""
    return dict(zip(source_names(song), song.channels, strict=True))


# The SMPS2ASM macro of each coordination flag (the name printed), and the aliases an assembly
# may also use (_smps2asm_inc.asm)
_FLAG_MACROS = {
    CoordFlag.PAN: "smpsPan", CoordFlag.DETUNE: "smpsAlterNote", CoordFlag.NOP: "smpsNop",
    CoordFlag.CHAN_TEMPO_DIV: "smpsChanTempoDiv", CoordFlag.ALTER_VOL: "smpsAlterVol",
    CoordFlag.NOTE_FILL: "smpsNoteFill", CoordFlag.CHANGE_TRANSPOSITION: "smpsChangeTransposition",
    CoordFlag.SET_TEMPO_MOD: "smpsSetTempoMod", CoordFlag.SET_TEMPO_DIV: "smpsSetTempoDiv",
    CoordFlag.SET_VOICE: "smpsSetvoice", CoordFlag.MOD_SET: "smpsModSet", CoordFlag.MOD_ON: "smpsModOn",
    CoordFlag.PSG_FORM: "smpsPSGform", CoordFlag.MOD_OFF: "smpsModOff", CoordFlag.PSG_VOICE: "smpsPSGvoice",
    CoordFlag.SET_VOL: "smpsSetVol",        # not Sonic 1's SMPS2ASM: this project's spelling
    CoordFlag.DAC_SAMPLE: "smpsDacSample",  # likewise
    CoordFlag.VOLUME_STEP: "smpsVolStep", CoordFlag.ALTER_VOLUME_STEP: "smpsAlterVolStep",
    CoordFlag.DETUNE_ADD: "smpsDetuneAdd", CoordFlag.GATE: "smpsGate",
    CoordFlag.VOICE_REGISTER: "smpsVoiceRegister", CoordFlag.FM3_SPECIAL: "smpsFm3Special",
}
_FLAG_ALIASES = {
    "smpsDetune": CoordFlag.DETUNE, "smpsAlterPitch": CoordFlag.CHANGE_TRANSPOSITION,
    "smpsFMvoice": CoordFlag.SET_VOICE, "smpsPSGAlterVol": CoordFlag.ALTER_VOL,
}
_MACRO_FLAGS = {**{m: f for f, m in _FLAG_MACROS.items()}, **_FLAG_ALIASES}


def flag_name(flag: CoordFlag) -> str:
    """A coordination flag's SMPS2ASM macro: CoordFlag.ALTER_VOL -> 'smpsAlterVol'."""
    return _FLAG_MACROS[flag]


def flag_from_macro(macro: str) -> CoordFlag | None:
    """The coordination flag an SMPS2ASM macro (or alias) writes; None for any other name."""
    return _MACRO_FLAGS.get(macro)


# The SMPS2ASM voice macro of each operator field (smpsVcAlgorithm / smpsVcFeedback set the voice's
# own fields; smpsVcUnusedBits is B0's top two bits, which the chip ignores)
_VOICE_MACROS = {
    "smpsVcDetune": VoiceField.DETUNE,
    "smpsVcCoarseFreq": VoiceField.MULTIPLE,
    "smpsVcRateScale": VoiceField.RATE_SCALE,
    "smpsVcAttackRate": VoiceField.ATTACK_RATE,
    "smpsVcAmpMod": VoiceField.AMP_MOD,
    "smpsVcDecayRate1": VoiceField.DECAY_RATE_1,
    "smpsVcDecayRate2": VoiceField.DECAY_RATE_2,
    "smpsVcDecayLevel": VoiceField.DECAY_LEVEL,
    "smpsVcReleaseRate": VoiceField.RELEASE_RATE,
    "smpsVcTotalLevel": VoiceField.TOTAL_LEVEL,
}


def voice_field_from_macro(macro: str) -> VoiceField | None:
    """The operator field an smpsVc* macro sets; None for any other macro."""
    return _VOICE_MACROS.get(macro)


def voice_macro(field_: VoiceField) -> str:
    """The smpsVc* macro that sets an operator field."""
    return next(m for m, f in _VOICE_MACROS.items() if f == field_)
