"""Sonic 1 sound driver lookup tables, regenerated from the driver source.

Everything here is a transcription of `sonic_1/s1.sounddriver.asm`, not a
recomputation from music theory.  That matters: the driver's note tables are
what the hardware actually plays, and they differ from equal temperament in
ways that are audible (see PSG_FREQUENCIES below).

Line references are to `sonic_1/s1.sounddriver.asm`.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Frequency tables
# ---------------------------------------------------------------------------

# The chip sample rates the driver's table-generation macros divide by.
# FM  = YM2612 master clock / 144 = 7,670,454 / 144  (== OPN2.NATIVE_RATE)
# PSG = SN76489 clock / 16        = 3,579,545 / 16
FM_SAMPLE_RATE = 53267
PSG_SAMPLE_RATE = 223721.5625


def _round_half_up(x: float) -> int:
    """Round half away from zero, as the assembler's roundFloatToInteger does.

    Python's built-in round() is banker's rounding and would disagree on exact
    .5 boundaries.
    """
    return math.floor(x + 0.5)


# --- FM ---------------------------------------------------------------------
#
# :1820-1837.  One octave of base frequencies, repeated 8 times with the block
# number added into bits 11-13:
#
#   MakeFMFrequency(f) = round(f * 1024*1024*2 / FM_Sample_Rate)
#   dc.w MakeFMFrequency(f) + octave*$800
#
# The row starts on B, not C — the driver's own comment explains this is to
# compensate for FMSetFreq subtracting $80 rather than $81, which makes the
# first table entry correspond to the 'rest' note.
#
# The resulting word IS the YM2612 $A4/$A0 register pair layout already
# (block << 11 | fnum), so it is written hi-then-lo with no fnum/block search.

_FM_BASE_OCTAVE = (
    15.39, 16.35, 17.34, 18.36, 19.45, 20.64,
    21.84, 23.13, 24.51, 25.98, 27.53, 29.15,
)

FM_FREQUENCIES: tuple[int, ...] = tuple(
    _round_half_up(f * 1024 * 1024 * 2 / FM_SAMPLE_RATE) + octave * 0x800
    for octave in range(8)
    for f in _FM_BASE_OCTAVE
)


# --- PSG --------------------------------------------------------------------
#
# :2086-2091.  MakePSGFrequency(f) = min($3FF, round(PSG_Sample_Rate / (f*2))).
#
# Transcribed verbatim, one tuple per octave row.  These are NOT exact octave
# doublings (row 2 starts at 522.71 where 130.98 * 4 = 523.92), so they must not
# be generated from a single base octave.
#
# The last row has only TEN entries, giving 70 in total.  Its final value,
# 223721.56, yields N = 1 — a degenerate ultrasonic period.  That entry is what
# `nMaxPSG` resolves to, and it is deliberate: Sonic 3 later replaced it with
# 6991.28 and appended two more.  Keep it as-is; SndAA, SndAB and SndAE rely on
# it sounding exactly this broken.

_PSG_ROWS = (
    (130.98, 138.78, 146.99, 155.79, 165.22, 174.78,
     185.19, 196.24, 207.91, 220.63, 233.52, 247.47),
    (261.96, 277.56, 293.59, 311.58, 329.97, 349.56,
     370.39, 392.49, 415.83, 440.39, 468.03, 494.95),
    (522.71, 556.51, 588.73, 621.44, 661.89, 699.12,
     740.79, 782.24, 828.59, 880.79, 932.17, 989.91),
    (1045.42, 1107.52, 1177.47, 1242.89, 1316.00, 1398.25,
     1491.47, 1575.50, 1669.55, 1747.82, 1864.34, 1962.46),
    (2071.49, 2193.34, 2330.42, 2485.78, 2601.40, 2796.51,
     2943.69, 3107.23, 3290.01, 3495.64, 3608.40, 3857.25),
    (4142.98, 4302.32, 4660.85, 4863.50, 5084.56, 5326.69,
     5887.39, 6214.47, 6580.02, 223721.56),
)


def _psg_n(freq: float) -> int:
    return min(0x3FF, _round_half_up(PSG_SAMPLE_RATE / (freq * 2)))


PSG_FREQUENCIES: tuple[int, ...] = tuple(
    _psg_n(f) for row in _PSG_ROWS for f in row
)

# Two SFX index past the end of the 70-entry table:
#   SndA2               nCs6 ($CA) -> index 73
#   SndB6 - Spikes Move nG6  ($D0) -> index 79
# On hardware the driver reads whatever ROM bytes follow PSGFrequencies (the
# CoordFlag routine's opcodes), which is not reproducible offline.  We continue
# the twelve-tone sequence from the last *musical* entry of the final row
# instead — deterministic and plausible.  Index 69 is left untouched so nMaxPSG
# keeps its authentic degenerate value.
_PSG_LAST_MUSICAL_INDEX = 68          # 6580.02, the last real pitch in row 5
_PSG_LAST_MUSICAL_FREQ = 6580.02

PSG_FREQUENCIES_EXTENDED: tuple[int, ...] = PSG_FREQUENCIES + tuple(
    _psg_n(_PSG_LAST_MUSICAL_FREQ * (2.0 ** ((i - _PSG_LAST_MUSICAL_INDEX) / 12.0)))
    for i in range(len(PSG_FREQUENCIES), 128)
)


# ---------------------------------------------------------------------------
# Note index derivation
# ---------------------------------------------------------------------------
#
# FMSetFreq  :393 — subi.b #$80  (so $80 becomes index 0, the rest sentinel)
# PSGSetFreq :1896 — subi.b #$81 (so $80 sets the carry flag and rests)
#
# Both then `add.b Transpose` and `andi.w #$7F`, so transposition WRAPS modulo
# 128 rather than clamping.  SndBC Teleport depends on this: its "invalid" $90
# transpose lands on the same index as the corrected $10.

def fm_note_index(note_value: int, transpose: int) -> int:
    """SMPS note byte + track transpose -> index into FM_FREQUENCIES."""
    return ((note_value - 0x80) + transpose) & 0x7F


def psg_note_index(note_value: int, transpose: int) -> int:
    """SMPS note byte + track transpose -> index into PSG_FREQUENCIES."""
    return ((note_value - 0x81) + transpose) & 0x7F


def psg_index_semitone(index: int) -> int:
    """Real pitch (SMPS semitone, C0 = 0) the PSG plays for a table index.

    Entries 0-68 are chromatic from C3 (index 0 = 851 = 131 Hz), so the pitch is index + 36.
    The driver masks the index to 7 bits and reads on past the table, so a note transposed
    below C3 (Credits PSG1: indices 125-127) or above nMaxPSG plays whatever word sits there -
    Marble Zone's five "data bug" notes, and Credits' G#3 where A2 was written.  Those come out
    of PSG_FREQUENCIES_EXTENDED like any other divider (0 clocked as 1).
    """
    import math
    if 0 <= index < 69:
        return 36 + index
    n = PSG_FREQUENCIES_EXTENDED[index & 0x7F]
    if n > 1:
        return round(57 + 12 * math.log2((3_579_545 / (32.0 * n)) / 440.0))
    # The table is followed by code, not data; where the extrapolated table has nothing usable
    # the written pitch is the best guess (the audit will show what the hardware really did).
    return 36 + index


# ---------------------------------------------------------------------------
# PSG volume envelopes — :43-60
# ---------------------------------------------------------------------------
#
# Indexed by `VoiceIndex - 1` (smpsPSGvoice is 1-based; 0 means "no envelope").
# One entry is consumed per tick and ADDED to the track volume as attenuation.
# $80 is the terminator: VolEnvHold rewinds the index so the previous value is
# held forever, and no volume write happens on that tick.
#
# NOTE: these are transcribed from the driver, not from configs/settings.yaml.
# That file's `fTone_07` is missing a leading zero (26 entries vs the driver's
# 27), which would shift PSG7's fade by one frame.

PSG_ENVELOPES: tuple[tuple[int, ...], ...] = (
    # PSG1
    (0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5, 6, 6, 6, 7, 0x80),
    # PSG2
    (0, 2, 4, 6, 8, 0x10, 0x80),
    # PSG3
    (0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 0x80),
    # PSG4
    (0, 0, 2, 3, 4, 4, 5, 5, 5, 6, 0x80),
    # PSG5
    (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
     2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 4, 0x80),
    # PSG6
    (3, 3, 3, 2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0x80),
    # PSG7
    (0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5,
     5, 6, 7, 0x80),
    # PSG8
    (0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4,
     4, 4, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 7, 7, 7, 0x80),
    # PSG9
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0xA, 0xB, 0xC, 0xD, 0xE, 0xF, 0x80),
)

ENVELOPE_TERMINATOR = 0x80


# ---------------------------------------------------------------------------
# FM register layout
# ---------------------------------------------------------------------------
#
# The SMPS voice macros list operators in the order OP4, OP3, OP2, OP1, which is
# the reverse of the register offsets.  Getting this backwards puts the OP1
# carrier into the self-feedback slot and produces severe distortion.

SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)

# FMSlotMask :2410 — which operators are carriers, per algorithm.  Bit i of the
# mask corresponds to FMInstrumentTLTable[i], whose register offsets are:
_TL_TABLE_OFFSETS = (0x00, 0x08, 0x04, 0x0C)
FM_SLOT_MASK = (8, 8, 8, 8, 0xA, 0xE, 0xE, 0xF)

CARRIER_OFFSETS_BY_ALG: tuple[tuple[int, ...], ...] = tuple(
    tuple(off for i, off in enumerate(_TL_TABLE_OFFSETS) if FM_SLOT_MASK[alg] & (1 << i))
    for alg in range(8)
)


# ---------------------------------------------------------------------------
# Channel maps
# ---------------------------------------------------------------------------

# SFX VoiceControl byte -> OPN2 channel index 0-5.  OPN2.key_on/key_off remap
# 3-5 to the $28 bit pattern 4-6 internally.
HW_FM_CHANNEL = {0x02: 2, 0x04: 3, 0x05: 4}

# SFX VoiceControl byte -> SN76489 channel index. $E0 is the noise channel,
# which a track becomes after smpsPSGform.
PSG_CHANNEL = {0x80: 0, 0xA0: 1, 0xC0: 2, 0xE0: 3}

# smpsPan operand names — _smps2asm_inc.asm:388-395.  YM2612 $B4 bit 7 = left,
# bit 6 = right.
PAN_VALUES = {
    'panNone':   0x00,
    'panRight':  0x40,
    'panLeft':   0x80,
    'panCentre': 0xC0,
    'panCenter': 0xC0,
}


# ---------------------------------------------------------------------------
# Self-check — these run at import and are the unit test for this module
# ---------------------------------------------------------------------------

assert len(FM_FREQUENCIES) == 96, len(FM_FREQUENCIES)
assert len(PSG_FREQUENCIES) == 70, len(PSG_FREQUENCIES)
assert len(PSG_FREQUENCIES_EXTENDED) == 128

# Known-good values from the assembled Sonic 1 driver.
assert FM_FREQUENCIES[0] == 0x025E, hex(FM_FREQUENCIES[0])
assert FM_FREQUENCIES[1] == 0x0284, hex(FM_FREQUENCIES[1])
assert FM_FREQUENCIES[2] == 0x02AB, hex(FM_FREQUENCIES[2])
assert FM_FREQUENCIES[11] == 0x047C, hex(FM_FREQUENCIES[11])
assert FM_FREQUENCIES[12] == 0x025E + 0x800, hex(FM_FREQUENCIES[12])

assert PSG_FREQUENCIES[0] == 0x0356, hex(PSG_FREQUENCIES[0])
assert PSG_FREQUENCIES[1] == 0x0326, hex(PSG_FREQUENCIES[1])
assert PSG_FREQUENCIES[11] == 0x01C4, hex(PSG_FREQUENCIES[11])
assert PSG_FREQUENCIES[69] == 1, PSG_FREQUENCIES[69]      # nMaxPSG degenerate entry

# Extension must leave the real table untouched.
assert PSG_FREQUENCIES_EXTENDED[:70] == PSG_FREQUENCIES

assert CARRIER_OFFSETS_BY_ALG[0] == (0x0C,)
assert CARRIER_OFFSETS_BY_ALG[4] == (0x08, 0x0C)
assert set(CARRIER_OFFSETS_BY_ALG[5]) == {0x04, 0x08, 0x0C}
assert len(CARRIER_OFFSETS_BY_ALG[7]) == 4
