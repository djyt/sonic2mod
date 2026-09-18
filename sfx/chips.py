"""Register-level chip writes, mirroring the Sonic 1 driver's output routines.

These deliberately do NOT reuse `ym2612.voice.program_voice` or the SN76489
wrapper's convenience writers, because both differ from the driver in ways that
matter on a continuous timeline:

* `program_voice` hardcodes $B4 to centre (clobbering smpsPan), clamps TL at 127
  where the driver wraps modulo 256, and writes SSG-EG registers the driver
  never touches.
* `write_tone_freq` / `write_volume` clamp their arguments; the driver truncates.
  Modulation sweeps routinely push the frequency word past $3FF, and the
  truncation is audible.

Line references are to `sonic_1/s1.sounddriver.asm`.
"""

from __future__ import annotations

from core.driver_tables import CARRIER_OFFSETS_BY_ALG, SMPS_OP_TO_REG_OFFSET
from ym2612.voice import _parse_op_vals

# YM2612 key-on/off register (global, always port 0 — not bank-switched)
_REG_KEY_ON = 0x28


def _bank_and_offset(track) -> tuple[int, int]:
    """WriteFMIorII: bit 2 of VoiceControl selects the port; bits 0-1 the channel."""
    ch = track.hw_ch
    return ch // 3, ch % 3


# ---------------------------------------------------------------------------
# YM2612
# ---------------------------------------------------------------------------

def fm_send_voice(opn2, track, voice) -> None:
    """Upload a full voice — mirror of SetVoice (:2366).

    Writes $B0, the 20 operator registers, the 4 TL registers with track volume
    added to carriers, then $B4 from the track's stored pan.  That last write is
    why smpsPan survives a later smpsSetvoice.
    """
    bank, c = _bank_and_offset(track)
    p = voice.params

    track.voice = voice
    opn2.write_reg(0xB0 + c, ((voice.feedback & 0x7) << 3) | (voice.algorithm & 0x7), bank=bank)

    detune = _parse_op_vals(p.get('smpsVcDetune'))
    mul    = _parse_op_vals(p.get('smpsVcCoarseFreq'))
    ks     = _parse_op_vals(p.get('smpsVcRateScale'))
    ar     = _parse_op_vals(p.get('smpsVcAttackRate'))
    am     = _parse_op_vals(p.get('smpsVcAmpMod'))
    dr     = _parse_op_vals(p.get('smpsVcDecayRate1'))
    sr     = _parse_op_vals(p.get('smpsVcDecayRate2'))
    sl     = _parse_op_vals(p.get('smpsVcDecayLevel'))
    rr     = _parse_op_vals(p.get('smpsVcReleaseRate'))
    tl     = _parse_op_vals(p.get('smpsVcTotalLevel'))

    for op in range(4):
        base = c + SMPS_OP_TO_REG_OFFSET[op]
        opn2.write_reg(0x30 + base, ((detune[op] & 0x7) << 4) | (mul[op] & 0xF), bank=bank)
        opn2.write_reg(0x50 + base, ((ks[op] & 0x3) << 6) | (ar[op] & 0x1F), bank=bank)
        opn2.write_reg(0x60 + base, ((am[op] & 0x1) << 7) | (dr[op] & 0x1F), bank=bank)
        opn2.write_reg(0x70 + base, sr[op] & 0x1F, bank=bank)
        opn2.write_reg(0x80 + base, ((sl[op] & 0xF) << 4) | (rr[op] & 0xF), bank=bank)

    carriers = CARRIER_OFFSETS_BY_ALG[voice.algorithm & 0x7]
    for op in range(4):
        off = SMPS_OP_TO_REG_OFFSET[op]
        value = tl[op] & 0xFF
        if off in carriers:
            # add.b — wraps modulo 256.  The chip's TL field is 7 bits, so bit 7
            # is discarded; this is NOT the same as clamping to 127.
            value = (value + track.volume) & 0xFF
        opn2.write_reg(0x40 + c + off, value, bank=bank)

    opn2.write_reg(0xB4 + c, track.ams_fms_pan, bank=bank)


def fm_send_tl(opn2, track) -> None:
    """Re-send carrier TLs after smpsAlterVol — mirror of SendVoiceTL (:2421).

    Differs from fm_send_voice: only carriers are written at all, and a write is
    skipped entirely when TL + volume carries past $FF.
    """
    voice = track.voice
    if voice is None:
        return
    if track.volume & 0x80:          # bmi — negative volume aborts the whole upload
        return

    bank, c = _bank_and_offset(track)
    tl = _parse_op_vals(voice.params.get('smpsVcTotalLevel'))
    carriers = CARRIER_OFFSETS_BY_ALG[voice.algorithm & 0x7]

    for op in range(4):
        off = SMPS_OP_TO_REG_OFFSET[op]
        if off not in carriers:
            continue
        value = (tl[op] & 0xFF) + track.volume
        if value > 0xFF:             # bcs — overflow skips the write
            continue
        opn2.write_reg(0x40 + c + off, value, bank=bank)


def fm_set_freq(opn2, track, word: int) -> None:
    """Write $A4 (block+fnum high) then $A0 (fnum low) — FMUpdateFreq."""
    bank, c = _bank_and_offset(track)
    word &= 0xFFFF
    opn2.write_reg(0xA4 + c, (word >> 8) & 0xFF, bank=bank)
    opn2.write_reg(0xA0 + c, word & 0xFF, bank=bank)


def fm_set_pan(opn2, track) -> None:
    bank, c = _bank_and_offset(track)
    opn2.write_reg(0xB4 + c, track.ams_fms_pan, bank=bank)


def fm_key_on(opn2, track) -> None:
    """$28 <- VoiceControl | $F0 — all four slots on (FMNoteOn :2700)."""
    opn2.write_reg(_REG_KEY_ON, (track.voice_control | 0xF0) & 0xFF, bank=0)


def fm_key_off(opn2, track) -> None:
    """$28 <- VoiceControl, slot bits clear (SendFMNoteOff :2711)."""
    opn2.write_reg(_REG_KEY_ON, track.voice_control & 0x07, bank=0)


# ---------------------------------------------------------------------------
# SN76489
# ---------------------------------------------------------------------------

def psg_set_freq(sn, track, word: int) -> None:
    """Latch a tone period — mirror of PSGUpdateFreq (:1927).

    A track that has run smpsPSGform has VoiceControl $E0; its *frequency* still
    goes to tone channel 2 ($C0) while its volume goes to the noise channel.
    """
    ch_bits = 0xC0 if track.voice_control == 0xE0 else track.voice_control
    word &= 0xFFFF
    sn.write(ch_bits | (word & 0x0F))
    sn.write((word >> 4) & 0x3F)


def psg_set_volume(sn, track, attenuation: int) -> None:
    """PSGSendVolume (:1995): channel bits, then +$10 to mark it a volume command."""
    sn.write((track.voice_control | (attenuation & 0x0F)) + 0x10)


def psg_note_off(sn, track) -> None:
    """SendPSGNoteOff (:2003) — attenuation $F, i.e. a hard mute, not a release.

    The FixBugs branch also silences the noise channel when PSG3 stops.  Without
    it a smpsPSGform SFX leaves noise ringing for the rest of the render.
    """
    value = (track.voice_control | 0x1F) & 0xFF
    sn.write(value)
    if value == 0xDF:
        sn.write(0xFF)


def psg_set_noise(sn, byte: int) -> None:
    """cfSetPSGNoise (:2196).  Always $E7 in Sonic 1's SFX: white noise, rate 3
    (period follows tone channel 2).  Resets the LFSR, as on hardware."""
    sn.write(byte & 0xFF)


def psg_silence_all(sn) -> None:
    """PSGSilenceAll (:2055)."""
    for value in (0x9F, 0xBF, 0xDF, 0xFF):
        sn.write(value)
