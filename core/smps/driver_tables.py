"""What every SMPS driver here reads its tables by: note bytes to table indexes, the PSG
envelope's form, the FM register layout, channel maps.  No driver's tables: those are each
driver's (core/drivers; Sonic 1's in reference.py), and a song carries its own
(PlaybackRules, rules.py).

Line references are to `reference/smps_drivers/sonic_1/s1.sounddriver.asm`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..chips import CARRIER_OFFSETS_BY_ALG, MD_PSG_CLOCK

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

FM_TABLE_NC0 = 1         # an FM table's index of nC0: index 0 is the rest's (FMSetFreq subtracts $80)
C1_SEMITONE = 12         # nC1: where a MOD's note index 0 (C-1) plays, an octave above nC0


def fm_table_index(semitone: int) -> int:
    """Where an SMPS semitone (nC0 = 0) stands in an FM table."""
    return semitone + FM_TABLE_NC0


def fm_note_index(note_value: int, transpose: int) -> int:
    """SMPS note byte + track transpose -> index into the driver's FM table."""
    return ((note_value - 0x80) + transpose) & 0x7F


def psg_note_index(note_value: int, transpose: int) -> int:
    """SMPS note byte + track transpose -> index into the driver's PSG table."""
    return ((note_value - 0x81) + transpose) & 0x7F


def psg_index_semitone(index: int, psg_read: tuple[int, ...]) -> int:
    """Real pitch (SMPS semitone, C0 = 0) the PSG plays for a table index (`psg_read`: the driver's
    words, PlaybackRules.psg_read).

    Entries 0-68 are chromatic from C3 (index 0 = 854 = 131 Hz), so the pitch is index + 36.
    The driver masks the index to 7 bits and reads on past the table, so a note transposed
    below C3 (Credits PSG1: indices 125-127) or above nMaxPSG plays whatever word sits there -
    Marble Zone's five "data bug" notes, and Credits' G#3 where A2 was written.  Those come out
    of the driver's words past its table like any other divider (0 clocked as 1).
    """
    if 0 <= index < 69:
        return 36 + index
    n = psg_read[index & 0x7F]
    if n > 1:
        return round(57 + 12 * math.log2((MD_PSG_CLOCK / (32.0 * n)) / 440.0))
    # The table is followed by code, not data; where the extrapolated table has nothing usable
    # the written pitch is the best guess (the audit will show what the hardware really did).
    return 36 + index


def psg_tone2_divider(note_value: int, transpose: int, psg_read: tuple[int, ...]) -> int:
    """Tone-2 divider the driver writes for a note — what clocks a rate-3 noise LFSR.

    nMaxPSG's table entry is divider 0, which the Sega VDP PSG clocks as N=1.
    """
    return max(1, psg_read[psg_note_index(note_value, transpose)])


# ---------------------------------------------------------------------------
# PSG volume envelopes
# ---------------------------------------------------------------------------

def psg_voice_name(byte: int) -> str:
    """smpsPSGvoice's byte as the music files spell it: 0 (no envelope) "$00", n "fTone_0n"."""
    return f"fTone_{byte:02d}" if byte else "$00"


@dataclass(frozen=True)
class PsgEnvelope:
    """A PSG volume envelope: one attenuation step a frame from the note's start, then the last
    step held (`loop_to` None: Sonic 1's $80, Type 1a's $83) or the steps from `loop_to` played
    again and again (Type 1a's $80 restart, $85 nn jump)."""

    steps: tuple[int, ...]
    loop_to: int | None = None

    @property
    def loops(self) -> bool:
        return self.loop_to is not None

    def frames(self, n: int) -> list[int]:
        """The steps the first `n` frames play.  A held envelope gives just its steps: whoever
        plays them holds the last."""
        if self.loop_to is None:
            return list(self.steps)
        out, body = list(self.steps), self.steps[self.loop_to:]
        while len(out) < n:
            out.extend(body)
        return out[:max(n, len(self.steps))]


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

# The driver's mask names the chip's carriers (core.chips.CARRIER_OFFSETS_BY_ALG), table order kept
assert tuple(
    tuple(off for i, off in enumerate(_TL_TABLE_OFFSETS) if FM_SLOT_MASK[alg] & (1 << i)) for alg in range(8)
) == CARRIER_OFFSETS_BY_ALG, "FMSlotMask disagrees with the YM2612's carriers"


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

assert CARRIER_OFFSETS_BY_ALG[0] == (0x0C,)
assert CARRIER_OFFSETS_BY_ALG[4] == (0x08, 0x0C)
assert set(CARRIER_OFFSETS_BY_ALG[5]) == {0x04, 0x08, 0x0C}
assert len(CARRIER_OFFSETS_BY_ALG[7]) == 4


def noise_envelope_frames(envelope: PsgEnvelope | None, base_volume: int = 0) -> int | None:
    """Frames a noise note sounds for: its envelope, then the ramp to attenuation 15 the
    renderer adds so the sample ends in silence (sn76489.sample_generator).  None = no envelope,
    or one that loops: the note sounds as long as it is keyed."""
    if envelope is None or not envelope.steps or envelope.loops:
        return None
    held_att = min(15, base_volume + envelope.steps[-1])
    return len(envelope.steps) + max(0, 15 - held_att) + 1


def chip_pitch(source_semitone: int, transpose: int, is_psg: bool, psg_read: tuple[int, ...]) -> int:
    """The real pitch (SMPS semitone, C0 = 0) the chip plays for a note byte and transpose.

    `transpose` is the driver's: the header pitch_offset plus every smpsChangeTransposition.
    A PSG note goes through the driver's frequency table, so one transposed past either end
    of it lands on whatever the hardware reads there.
    """
    return (psg_index_semitone(source_semitone + transpose, psg_read) if is_psg
            else source_semitone + transpose)
