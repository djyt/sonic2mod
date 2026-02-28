"""YM2612 sample generator — Segment 4 of the YM2612 synthesis pipeline.

Reads the song's FM voices and per-song voice_instrument_map, calls render_note()
for each InstrumentRange entry that has a root anchor, and returns populated
{instrument_number: (pcm_bytes, sample_rate_hz)} pairs ready for MOD file assembly.

Public API::

    from ym2612.sample_generator import generate_fm_samples

    samples = generate_fm_samples(song, config, synth)
    # samples = {inst_num: (pcm_bytes, target_rate_hz), ...}

Usage (smoke test)::

    python ym2612/sample_generator.py
"""

from __future__ import annotations

import struct
import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from tables import PERIOD_TABLE                    # noqa: E402
from mod import ModSample                          # noqa: E402
from smps_parser import SmpsVoice, SmpsSong        # noqa: E402
from config import ConversionConfig, SynthesisSettings, InstrumentRange  # noqa: E402
from ym2612.wrapper import OPN2                    # noqa: E402
from ym2612.renderer import render_note            # noqa: E402


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_fm_samples(
    song: SmpsSong,
    config: ConversionConfig,
    synth: SynthesisSettings,
) -> dict:
    """Render FM samples for every InstrumentRange entry that has a root anchor.

    Args:
        song:   Parsed SmpsSong — provides song.voices (list[SmpsVoice]).
        config: ConversionConfig — provides voice_instrument_map.
        synth:  SynthesisSettings — clock/amiga_clock/sustain/release.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM.
        Only entries with entry.root set are included.
    """
    voice_lookup = {v.index: v for v in song.voices}
    result: dict[int, tuple[bytes, int]] = {}

    # Create one OPN2 instance — render_note resets it on each call
    opn2 = OPN2(mode=synth.mode)

    for voice_idx, range_list in config.voice_instrument_map.items():
        if voice_idx not in voice_lookup:
            print(f"  Warning: voice {voice_idx} not found in song, skipping")
            continue
        voice = voice_lookup[voice_idx]

        for entry in range_list:
            if entry.root is None:
                continue  # no anchor → can't determine target_rate

            mod_note_idx = entry.root.value  # 0–35
            target_rate = round(synth.amiga_clock / PERIOD_TABLE[mod_note_idx])

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                pcm_bytes, _ = render_note(
                    voice,
                    mod_note_idx,
                    sustain_secs=synth.sustain,
                    release_secs=synth.release,
                    target_rate=target_rate,
                    opn2=opn2,
                    clock_rate=synth.clock_rate,
                )

            if not pcm_bytes:
                print(f"  Warning: instrument {entry.instrument} (voice {voice_idx}, "
                      f"root={entry.root.name}) rendered empty — skipping")
                continue

            for w in caught:
                if issubclass(w.category, UserWarning) and "silence" in str(w.message):
                    print(f"  Warning: instrument {entry.instrument} rendered silence")

            print(f"  Instrument {entry.instrument:2d}: voice={voice_idx}, "
                  f"root={entry.root.name} (idx={mod_note_idx}), "
                  f"rate={target_rate} Hz, {len(pcm_bytes)} bytes")

            result[entry.instrument] = (pcm_bytes, target_rate)

    return result


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_mod_sample(pcm_bytes: bytes, target_rate: int, volume: int = 64) -> ModSample:
    """Wrap rendered PCM bytes in a ModSample ready for insertion into ModFile.

    Args:
        pcm_bytes:   8-bit signed mono PCM from render_note.
        target_rate: Sample rate in Hz (informational; stored in result).
        volume:      MOD volume 0–64 (default 64).

    Returns:
        Populated ModSample (name, length, volume, data).
    """
    sample = ModSample(f"ym2612@{target_rate}Hz")
    sample.data = pcm_bytes
    sample.length = len(pcm_bytes) // 2   # MOD length is in words
    sample.set_volume(volume)
    return sample


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render voice 1 (FM2 bass) from Title Screen using a minimal fake config."""

    # Replicate voice 1 from renderer.py smoke test
    voice1 = SmpsVoice(
        index=1,
        algorithm=0x00,
        feedback=0x04,
        params={
            'smpsVcDetune':      '$03, $03, $03, $03',
            'smpsVcCoarseFreq':  '$01, $00, $05, $06',
            'smpsVcRateScale':   '$02, $02, $03, $03',
            'smpsVcAttackRate':  '$1F, $1F, $1F, $1F',
            'smpsVcAmpMod':      '$00, $00, $00, $00',
            'smpsVcDecayRate1':  '$06, $09, $06, $07',
            'smpsVcDecayRate2':  '$08, $06, $06, $07',
            'smpsVcDecayLevel':  '$0F, $01, $01, $02',
            'smpsVcReleaseRate': '$0F, $0F, $0F, $0F',
            'smpsVcTotalLevel':  '$00, $13, $37, $19',
        },
    )

    # Minimal fake SmpsSong
    from smps_parser import SmpsSong, SmpsSongHeader
    fake_song = SmpsSong(
        header=SmpsSongHeader(voice_label="test"),
        voices=[voice1],
    )

    # Minimal ConversionConfig with voice_instrument_map for voice 1
    from tables import ModNote
    fake_config = ConversionConfig()
    fake_config.voice_instrument_map = {
        1: [
            InstrumentRange(low=0, high=95, instrument=5, root=ModNote.A3),
        ]
    }

    synth = SynthesisSettings()

    print("Smoke test — generate_fm_samples(voice=1/FM2-bass, root=A3)...")
    print(f"  amiga_clock = {synth.amiga_clock}")
    print(f"  sustain     = {synth.sustain}s, release = {synth.release}s")
    print()

    samples = generate_fm_samples(fake_song, fake_config, synth)

    if not samples:
        print("  ERROR: no samples generated")
        sys.exit(1)

    print()
    print(f"  Generated {len(samples)} instrument(s)")
    for inst_num, (pcm, rate) in samples.items():
        print(f"    instrument {inst_num}: {len(pcm)} bytes @ {rate} Hz")

    # Write instrument 5 as 16-bit raw for Audacity
    if 5 in samples:
        pcm, rate = samples[5]
        # Convert 8-bit signed (stored as uint8 via & 0xFF) → 16-bit for Audacity
        # Interpret each byte as signed int8 then scale to int16
        raw16 = bytearray(len(pcm) * 2)
        for i, b in enumerate(pcm):
            val8 = b if b < 128 else b - 256   # uint8 → int8
            val16 = max(-32768, min(32767, val8 * 256))
            struct.pack_into('<h', raw16, i * 2, val16)

        out_path = Path(__file__).parent.parent / "output" / "sample_gen_test.raw"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_bytes(bytes(raw16))

        print()
        print(f"  Written: {out_path}  ({len(raw16)} bytes, 16-bit for Audacity)")
        print()
        print("Load in Audacity:  File > Import > Raw Data")
        print("  Encoding  : Signed 16-bit PCM")
        print("  Byte order: Little-endian")
        print("  Channels  : 1 (Mono)")
        print(f"  Sample rate: {rate}")
        print()
        print("  SUCCESS")
    else:
        print("  WARNING: instrument 5 not in output")
        sys.exit(1)


if __name__ == "__main__":
    _smoke_test()
