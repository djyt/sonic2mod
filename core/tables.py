from enum import Enum

# https://eab.abime.net/showthread.php?t=66065
# Borrowed from: https://github.com/8bitbubsy/pt2-clone/blob/master/src/pt2_tables.c
PERIOD_TABLE = [
#   C-1  C#1  D-1  D#1  E-1  F-1  F#1  G-1  G#1  A-1  A#1  B-1
    856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
#   C-2  C#2  D-2  D#2  E-2  F-2  F#2  G-2  G#2  A-2  A#2  B-2
    428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
#   C-3  C#3  D-3  D#3  E-3  F-3  F#3  G-3  G#3  A-3  A#3  B-3
    214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113, 0,
]


class ModNote(Enum):
    C1 = 0
    C1s = 1
    D1 = 2
    D1s = 3
    E1 = 4
    F1 = 5
    F1s = 6
    G1 = 7
    G1s = 8
    A1 = 9
    A1s = 10
    B1 = 11

    C2 = 12
    C2s = 13
    D2 = 14
    D2s = 15
    E2 = 16
    F2 = 17
    F2s = 18
    G2 = 19
    G2s = 20
    A2 = 21
    A2s = 22
    B2 = 23

    C3 = 24
    C3s = 25
    D3 = 26
    D3s = 27
    E3 = 28
    F3 = 29
    F3s = 30
    G3 = 31
    G3s = 32
    A3 = 33
    A3s = 34
    B3 = 35


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

_CHROMATIC_NAMES = ['C', 'Cs', 'D', 'Ds', 'E', 'Es', 'Fs', 'G', 'Gs', 'A', 'As', 'B']

def _semitone_to_name(semitone: int) -> str:
    """Return SMPS primary note name for a semitone offset from C0 (e.g. 84 → 'C7')."""
    octave = semitone // 12
    note   = semitone % 12
    return f"{_CHROMATIC_NAMES[note]}{octave}"


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
# SMPS DAC sample names → byte values (Sonic 1)
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


def smps_note_to_mod_note(note_value, transpose=0, channel_name=None, voice_idx=None,
                          warn_fn=None, extra_ctx=None):
    """Convert SMPS note byte to ModNote.

    Args:
        note_value: SMPS note byte (0x81 = C0, 0x82 = C#0, etc.)
        transpose: Semitone offset to add
        channel_name: Optional channel label for warning messages (e.g. "FM1")
        voice_idx: Optional current voice index for warning messages
        warn_fn: Optional callback(dict) for structured warnings; if None, prints to stdout
        extra_ctx: Optional extra context string passed through to warn_fn (e.g. PSG voice label)

    Returns:
        ModNote enum value, or None if out of range
    """
    semitone = (note_value - 0x81) + transpose
    if semitone < 0:
        src_name      = _semitone_to_name(note_value - 0x81)
        boundary_name = _semitone_to_name(-transpose)
        if warn_fn:
            warn_fn({
                'type': 'clamp_low',
                'channel': channel_name,
                'voice_idx': voice_idx,
                'extra_ctx': extra_ctx,
                'src_name': src_name,
                'note_value': note_value,
                'transpose': transpose,
                'boundary': boundary_name,
            })
        else:
            chan_info  = f" [{channel_name}]" if channel_name else ""
            voice_hint = f" for voice {voice_idx}" if voice_idx is not None else ""
            print(f"Warning{chan_info}: n{src_name} ({note_value:#x}) + transpose {transpose} = semitone {semitone}, clamped to C1")
            print(f"  -> source notes below {boundary_name} clamp with transpose {transpose}; add map entry{voice_hint}: high: {src_name}")
        semitone = 0
    elif semitone > 35:
        src_name      = _semitone_to_name(note_value - 0x81)
        boundary_name = _semitone_to_name(35 - transpose)
        if warn_fn:
            warn_fn({
                'type': 'clamp_high',
                'channel': channel_name,
                'voice_idx': voice_idx,
                'extra_ctx': extra_ctx,
                'src_name': src_name,
                'note_value': note_value,
                'transpose': transpose,
                'boundary': boundary_name,
            })
        else:
            chan_info  = f" [{channel_name}]" if channel_name else ""
            voice_hint = f" for voice {voice_idx}" if voice_idx is not None else ""
            print(f"Warning{chan_info}: n{src_name} ({note_value:#x}) + transpose {transpose} = semitone {semitone}, clamped to B3")
            print(f"  -> source notes above {boundary_name} clamp with transpose {transpose}; add map entry{voice_hint}: low: {src_name}")
        semitone = 35
    return ModNote(semitone)
