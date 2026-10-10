"""Translate SmpsVoice operator data to YM2612 register writes.

Public API::

    from ym2612.voice import program_voice

    program_voice(opn2, voice, channel=0)

This writes all operator and channel-level registers for the voice.
It does NOT set frequency or trigger key-on — call those separately::

    opn2.write_reg(0xA4 + ch_in_bank, fnum_hi, bank=bank)
    opn2.write_reg(0xA0 + ch_in_bank, fnum_lo, bank=bank)
    opn2.key_on(channel)

Usage::

    python ym2612/voice.py   # runs smoke test with Title Screen voice 0
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing smps_parser (project root) when run as a script or module
_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.chips import REG_FEEDBACK_ALGORITHM, REG_LFO
from core.smps import SmpsVoice, VoiceField
from ym2612.wrapper import OPN2

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
    opn2.write_reg(0xB4 + ch_in_bank, _BOTH_SPEAKERS | sensitivity, bank=bank)

    # The operators, as the driver writes them (the track volume on the carriers)
    for reg, value in voice.registers(tl_offset).items():
        opn2.write_reg(reg + ch_in_bank, value, bank=bank)


# ---------------------------------------------------------------------------
# Smoke test — Title Screen voice 0 on channel 0, A4, 100 ms
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Smoke test: program Title Screen voice 0, render 100 ms, check peak > 0."""

    # Title Screen voice 0 (from Mus8A - Title Screen.asm)
    voice = SmpsVoice(
        index=0,
        algorithm=0x02,
        feedback=0x07,
        operators={
            VoiceField.DETUNE:      (0x00, 0x05, 0x00, 0x05),
            VoiceField.MULTIPLE:  (0x02, 0x01, 0x08, 0x01),
            VoiceField.RATE_SCALE:   (0x00, 0x00, 0x00, 0x00),
            VoiceField.ATTACK_RATE:  (0x10, 0x1E, 0x1E, 0x1E),
            VoiceField.AMP_MOD:      (0x00, 0x00, 0x00, 0x00),
            VoiceField.DECAY_RATE_1:  (0x0F, 0x1F, 0x1F, 0x1F),
            VoiceField.DECAY_RATE_2:  (0x02, 0x00, 0x00, 0x00),
            VoiceField.DECAY_LEVEL:  (0x01, 0x00, 0x00, 0x00),
            VoiceField.RELEASE_RATE: (0x0F, 0x0F, 0x0F, 0x0F),
            VoiceField.TOTAL_LEVEL:  (0x01, 0x22, 0x24, 0x18),
        },
    )

    detune = voice.operator_values(VoiceField.DETUNE)
    mul    = voice.operator_values(VoiceField.MULTIPLE)
    tl     = voice.operator_values(VoiceField.TOTAL_LEVEL)
    ar     = voice.operator_values(VoiceField.ATTACK_RATE)
    dr     = voice.operator_values(VoiceField.DECAY_RATE_1)

    print("Smoke test — programming Title Screen voice 0 onto channel 0...")
    print(f"  algorithm={voice.algorithm}  feedback={voice.feedback}")
    print(f"  detune ={detune}")
    print(f"  mul    ={mul}")
    print(f"  tl     ={tl}")
    print(f"  ar     ={ar}")
    print(f"  dr     ={dr}")

    channel = 0
    opn2 = OPN2(mode="ym2612")

    reg_count = [0]
    _orig_write = opn2.write_reg
    def _counting_write(addr, data, bank=0):
        reg_count[0] += 1
        _orig_write(addr, data, bank=bank)
    opn2.write_reg = _counting_write

    program_voice(opn2, voice, channel)

    ch_regs = 2         # 0xB0, 0xB4
    op_regs = 4 * 7     # 7 registers × 4 operators
    total   = reg_count[0]
    print(f"  Registers written: {ch_regs} channel + {op_regs} operator = {total} total")
    assert total == 30, f"Expected 30 register writes, got {total}"

    # Restore and set A4 frequency (fnum=541, block=4 — same as validate.py)
    opn2.write_reg = _orig_write
    bank       = channel // 3
    ch_in_bank = channel % 3
    fnum  = 541
    block = 4
    opn2.write_reg(0xA4 + ch_in_bank, (block << 3) | (fnum >> 8), bank=bank)
    opn2.write_reg(0xA0 + ch_in_bank, fnum & 0xFF,                bank=bank)

    n_samples = OPN2.NATIVE_RATE // 10  # ≈ 5327 samples = 100 ms
    print(f"  Rendering {n_samples} samples (~100ms) after key-on...")
    opn2.key_on(channel)
    samples = opn2.render_samples(n_samples)

    peak = max(max(abs(l), abs(r)) for l, r in samples)
    print(f"  Peak: {peak}", end="  ")

    if peak > 0:
        print("SUCCESS")
    else:
        print("WARNING: peak is 0 — chip produced silence")
        sys.exit(1)


if __name__ == "__main__":
    _smoke_test()
