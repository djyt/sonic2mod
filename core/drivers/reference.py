"""Sonic 1's sound driver tables, regenerated from the driver source, and the rules its songs play
by (SONIC1_RULES).  The reference: every driver here was compared with these and shares them where
it reads none of its own (Moonwalker its FM and PSG tables, Golden Axe its PSG table and envelopes,
Streets of Rage its PSG rows), so they stand beside the families rather than in sonic1/.

Everything here is a transcription of `reference/smps_drivers/sonic_1/s1.sounddriver.asm`, not a
recomputation from music theory.  That matters: the driver's note tables are what the hardware
actually plays, and they differ from equal temperament in ways that are audible (see
PSG_FREQUENCIES below).  Line references are to that file.
"""

from __future__ import annotations

import math

from core.chips import FM_SAMPLE_RATE, PSG_SAMPLE_RATE
from core.smps import SMPS_DAC_NAMES, PlaybackRules, PsgEnvelope, psg_voice_name

from .names import SmpsDriver

# ---------------------------------------------------------------------------
# Frequency tables
# ---------------------------------------------------------------------------

# The chip clocks and native rates the driver's table-generation macros divide by are the chips'
# (core/chips): FM 53267 Hz, PSG 223721.5625 Hz at the NTSC Mega Drive's clocks.


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

# The last three entries ARE known: a note transposed one to three semitones below the table
# (index -1..-3, masked to 127..125) is played by Spring Yard PSG1's opening riff and by Credits
# PSG1, and both recordings show the same dividers in the PSG register - the CoordFlag opcodes
# the driver reads there.  Index 125 is 0 (inaudible), 126 sounds B2, 127 G#3.
_PSG_MEASURED_TAIL = {125: 0, 126: 922, 127: 540}
PSG_FREQUENCIES_EXTENDED = tuple(_PSG_MEASURED_TAIL.get(i, n) for i, n in enumerate(PSG_FREQUENCIES_EXTENDED))


# ---------------------------------------------------------------------------
# PSG volume envelopes — :43-60
# ---------------------------------------------------------------------------
#
# Indexed by `VoiceIndex - 1` (smpsPSGvoice is 1-based; 0 means "no envelope").
# One entry is consumed per tick and ADDED to the track volume as attenuation.
# $80 is the terminator: VolEnvHold rewinds the index so the previous value is
# held forever, and no volume write happens on that tick.
#
# This is the one transcription: the SFX driver indexes PSG_ENVELOPES directly and
# the SN76489 synthesiser looks envelopes up by name in PSG_ENVELOPES_BY_NAME.
# (configs/settings.yaml used to carry its own copy, whose PSG7 was one zero short.)


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



# The same tables under the names the music files give smpsPSGvoice (fTone_01 … fTone_09,
# also the `envelope:` keys in a config's psg_map / psg_voice_map), without the terminator:
# the synthesiser steps one value per frame and holds the last one, which is what $80 means.
PSG_ENVELOPES_BY_NAME: dict[str, tuple[int, ...]] = {
    psg_voice_name(i + 1): table[:table.index(ENVELOPE_TERMINATOR)]
    for i, table in enumerate(PSG_ENVELOPES)
}


# A song's envelopes by smpsPSGvoice name; Sonic 1's for every asm song and VGM lift
SONIC1_ENVELOPES: dict[str, PsgEnvelope] = {name: PsgEnvelope(steps) for name, steps in PSG_ENVELOPES_BY_NAME.items()}



SONIC1_RULES = PlaybackRules(
    driver=SmpsDriver.SONIC1,
    fm_frequencies=FM_FREQUENCIES,
    psg_frequencies=PSG_FREQUENCIES,
    psg_read=PSG_FREQUENCIES_EXTENDED,
    psg_envelopes=SONIC1_ENVELOPES,
    dac_names={v: k for k, v in SMPS_DAC_NAMES.items()},     # dKick ...
)


# Self-check: these run at import and are the unit test for these tables

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
