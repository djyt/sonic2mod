"""Translate SmpsVoice operator data to YM2612 register writes (onto core.chips.ym2612.OPN2).

Public API::

    from core.synth.fm_voice import program_voice

    program_voice(opn2, voice, channel=0)

This writes all operator and channel-level registers for the voice.
It does NOT set frequency or trigger key-on — call those separately::

    opn2.write_reg(0xA4 + ch_in_bank, fnum_hi, bank=bank)
    opn2.write_reg(0xA0 + ch_in_bank, fnum_lo, bank=bank)
    opn2.key_on(channel)
"""

from __future__ import annotations

from ..chips import REG_FEEDBACK_ALGORITHM, REG_LFO, REG_PAN
from ..chips.ym2612 import OPN2
from ..smps import SmpsVoice

# The SMPS operator order comes from core.smps.driver_tables, transcribed from
# s1.sounddriver.asm — one source of truth shared with the SFX driver emulation in
# sfx/chips.py.  Getting the operator order backwards puts the OP1 carrier in the
# self-feedback slot (severe distortion).
#
# Every operator's TL is written as the voice states it, plus the track volume on the
# carriers exactly as the driver's SetVoice adds it (add.b: modulo 256, and the chip
# keeps 7 bits).  55 Sonic 1 voices put two or more carriers at TL 0 (every GHZ lead
# among them); at the channel's volume their sum may overflow the chip's 9-bit channel
# accumulator (Nuked's OPN2_ChGenerate clamp, hardware behaviour) and that clipping is
# part of the sound the game plays, so nothing here softens it — nor adds any: at TL 0
# a GHZ lead clips a third of its samples where the hardware, at FM1's +18, clips none.
# A lone carrier at TL 0 fills the accumulator exactly and can never clip.


_BOTH_SPEAKERS = 0xC0


def program_voice(opn2: OPN2, voice: SmpsVoice, channel: int, tl_offset: int = 0) -> None:
    """Program a SMPS voice onto a YM2612 channel.

    Writes all operator and channel-level registers for the voice.  Does NOT
    set frequency or trigger key-on — call those separately.

    Args:
        opn2:            Initialised OPN2 emulator instance.
        voice:           Parsed SMPS voice (SmpsVoice dataclass from smps_parser).
        channel:         YM2612 channel 0–5.
        tl_offset:       Track volume (smpsHeaderFM volume + smpsAlterVol, 0–127) added to the
                         carrier operators' TL the way SetVoice does; 0 = the bare voice.
    """
    bank       = channel // 3
    ch_in_bank = channel % 3

    # Channel-level registers: both speakers, and the LFO a voice plays under (its rate is the chip's)
    opn2.write_reg(REG_FEEDBACK_ALGORITHM + ch_in_bank, voice.feedback_algorithm, bank=bank)
    sensitivity = 0
    if voice.lfo is not None:
        opn2.write_reg(REG_LFO, voice.lfo.register)
        sensitivity = voice.lfo.sensitivity
    opn2.write_reg(REG_PAN + ch_in_bank, _BOTH_SPEAKERS | sensitivity, bank=bank)

    # The operators, as the driver writes them (the track volume on the carriers)
    for reg, value in voice.registers(tl_offset).items():
        opn2.write_reg(reg + ch_in_bank, value, bank=bank)


# ---------------------------------------------------------------------------
# Smoke test — Title Screen voice 0 on channel 0, A4, 100 ms
# ---------------------------------------------------------------------------
