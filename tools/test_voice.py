"""Render SLZ voice $05 (mod_instrument 9) at OPN2 native rate — no downsampling.

Writes output/test_voice.raw as 16-bit signed little-endian mono PCM so the
result can be loaded directly in Audacity without any sample-rate conversion
artefacts.  Lets you verify whether the synthesis itself sounds correct before
the lower-rate MOD target_rate (~9852 or ~19705 Hz) is brought into the picture.

Usage::

    python tools/test_voice.py

Load in Audacity:  File > Import > Raw Data
  Encoding   : Signed 16-bit PCM
  Byte order : Little-endian
  Channels   : 1 (Mono)
  Sample rate: 53267   (printed on completion)
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.smps_parser import SmpsParser
from ym2612.renderer import freq_to_fnum_block, note_to_freq, _render_raw, _set_freq, _to_mono
from ym2612.voice import program_voice
from ym2612.wrapper import OPN2

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ASM_FILE    = _ROOT / "sonic_1/music/Mus84 - SLZ.asm"
_VOICE_INDEX = 5          # voice $05 — the one used by mod_instrument 9

# synth_root = Fs8 → SMPS semitone 102 → mod_note_index = 102 - 12 = 90
# cumulative_transpose at voice $05 activation: pitch_offset(-12) + smpsAlterPitch $33(+51) = +39
# chip pitch = nEb4(51) + 39 + 12 = 102 = Fs8 ≈ 5924 Hz
_MOD_NOTE_INDEX = 90      # Fs8 (5924 Hz)

_SUSTAIN_SECS   = 2.0     # longer than normal — let the full envelope breathe
_RELEASE_SECS   = 0.5

# Match the synthesis settings used by sample_generator.py
_HEADROOM_DB    = 6.0
_CARRIER_BAL    = True
_CLOCK_RATE     = 7_670_454

_OUT_PATH = _ROOT / "output" / "test_voice.raw"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # --- load voice ---
    print(f"Parsing {_ASM_FILE.name} ...")
    song = SmpsParser().parse_file(str(_ASM_FILE))
    if _VOICE_INDEX >= len(song.voices):
        print(f"ERROR: voice index {_VOICE_INDEX} out of range "
              f"(song has {len(song.voices)} voices)")
        sys.exit(1)
    voice = song.voices[_VOICE_INDEX]
    print(f"  Voice ${_VOICE_INDEX:02X}  alg={voice.algorithm}  fb={voice.feedback}")

    # --- synthesis parameters ---
    native_rate  = _CLOCK_RATE // 6 // 24
    headroom_tl  = round(_HEADROOM_DB / 0.75)
    freq         = note_to_freq(_MOD_NOTE_INDEX)   # Ds4 ≈ 311 Hz
    fnum, block  = freq_to_fnum_block(freq, _CLOCK_RATE)

    print(f"  Synthesis freq   = {freq:.2f} Hz  (fnum={fnum}, block={block})")
    print(f"  Native rate      = {native_rate} Hz  (no downsampling)")
    print(f"  headroom_tl      = {headroom_tl}  carrier_balance = {_CARRIER_BAL}")

    # --- carrier count for balance TL ---
    from ym2612.voice import _CARRIER_OFFSETS_BY_ALG
    n_carriers   = len(_CARRIER_OFFSETS_BY_ALG[voice.algorithm & 0x7])
    balance_tl   = round(20 * math.log10(n_carriers) / 0.75) if (_CARRIER_BAL and n_carriers > 1) else 0
    total_boost  = headroom_tl + balance_tl
    print(f"  carriers         = {n_carriers}  balance_tl={balance_tl}  total boost={total_boost} TL steps")

    # --- render ---
    sustain_n = int(native_rate * _SUSTAIN_SECS)
    release_n = int(native_rate * _RELEASE_SECS)
    print(f"  Rendering {sustain_n + release_n} samples "
          f"({_SUSTAIN_SECS + _RELEASE_SECS:.1f}s) ...")

    opn2 = OPN2(mode="ym2612")
    program_voice(opn2, voice, 0, headroom_tl=headroom_tl, carrier_balance=_CARRIER_BAL)
    _set_freq(opn2, fnum, block, 0)
    raw  = _render_raw(opn2, sustain_n, release_n, 0)
    mono = _to_mono(raw)

    pre_peak = max(abs(v) for v in mono) if mono else 0
    print(f"  Pre-normalise peak: {pre_peak}")
    if pre_peak == 0:
        print("  WARNING: peak is 0 — chip produced silence")
        sys.exit(1)

    # --- write 16-bit PCM (avoids 8-bit staircase noise in Audacity) ---
    scale = 32767.0 / pre_peak
    buf   = bytearray(len(mono) * 2)
    for i, v in enumerate(mono):
        val = max(-32768, min(32767, round(v * scale)))
        struct.pack_into('<h', buf, i * 2, val)

    _OUT_PATH.parent.mkdir(exist_ok=True)
    _OUT_PATH.write_bytes(bytes(buf))

    print()
    print(f"Written: {_OUT_PATH}")
    print(f"  {len(buf):,} bytes  |  {len(mono):,} samples  |  {native_rate} Hz  |  16-bit mono")
    print()
    print("Load in Audacity:  File > Import > Raw Data")
    print("  Encoding   : Signed 16-bit PCM")
    print("  Byte order : Little-endian")
    print("  Channels   : 1 (Mono)")
    print(f"  Sample rate: {native_rate}")


if __name__ == "__main__":
    main()
