# SN76489 PSG Synthesis Pipeline

How PSG channels are synthesized into Amiga MOD samples using the SN76489 emulator.

Related docs: `docs/yaml_config.md` §PSG Instrument Mapping (YAML schema), `docs/smps_driver.md` §PSG Channels (driver internals), `docs/synthesis.md` (FM/YM2612 equivalent).

---

## Overview

```
psg_map / psg_voice_map entry  →  generate_psg_samples()  →  PCM mono  →  normalize  →  MOD sample
(PsgInstrumentEntry + root)       (sn76489/ package)          (int8)
```

Three entry types correspond to SN76489 output modes:

| Type | SN76489 mode | Typical use |
|------|-------------|-------------|
| `tone` | Square-wave tone channel | PSG1–PSG3 melodic voices |
| `white_noise` | White-noise LFSR | Snare, hi-hat, percussive noise |
| `periodic_noise` | Periodic noise LFSR | Buzzy bass, periodic pulse |

---

## Quick Start

1. Set `psg_synthesis.enabled: true` in `configs/settings.yaml`.
2. Ensure gcc or MSVC is on PATH (needed to compile `sn76489.c`).
3. Run smoke tests to verify the pipeline produces audible output.
4. Convert: `python convert.py --config configs/my_song.yaml`.

### Smoke tests

```bash
# DLL + chip: C3 tone + white noise → output/psg_{tone,noise}_test.raw
python sn76489/validate.py

# Renderer: C3 tone + white noise → output/psg_{tone,noise}_test.raw
python sn76489/renderer.py

# Full pipeline: fTone_04 envelope + white noise → output/psg_sample_gen_test_*.raw
python sn76489/sample_generator.py
```

Audacity import (all PSG raw files):
```
File > Import > Raw Data
  Encoding   : Signed 16-bit PCM
  Byte order : Little-endian
  Channels   : 1 (Mono)
  Sample rate: (use the rate printed to console by each script)
```

---

## Settings Reference

`configs/settings.yaml` — `psg_synthesis:` block:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Set `true`; requires gcc/MSVC for sn76489.c |
| `clock_rate` | int | `3579545` | NTSC Mega Drive SN76489 clock (Hz) |
| `amiga_clock` | int | `3546895` | PAL Amiga clock used for `target_rate` calc |
| `sustain_duration` | float | `1.0` | Seconds held before key-off |
| `release_padding` | float | `0.2` | Seconds captured after key-off |
| `psg_output_max` | int | `4096` | Tone peak amplitude from C emulator; white noise peaks at 2048 (halved in sn76489.c) |
| `psg_envelope_tables` | dict | `{}` | Named per-frame attenuation tables (`fTone_01`–`fTone_09`) |

---

## psg_map and psg_voice_map (YAML)

### psg_map

Keyed by `smpsPSGform` byte (hex or decimal). When `smpsPSGform $E7` appears in channel data, the PSG channel switches to this instrument.

The `type` field is **auto-inferred** from bit 2 of the byte: 0 = `periodic_noise`, 1 = `white_noise`. Use `type: tone` explicitly for tone entries (routed via `psg_voice_map` instead).

```yaml
psg_map:
  0xE7:                    # smpsPSGform byte; bit 2=1 → white noise
    mod_instrument: 7      # MOD instrument slot (1-based)
    root: A3               # MOD note anchor; determines target_rate
    noise_rate: 0          # Preset divider: 0=N/512, 1=N/1024, 2=N/2048 (rate 3 approximated as 0)
    envelope: fTone_04     # Named envelope from psg_envelope_tables, or inline list
    base_volume: 0         # SN76489 attenuation 0=max, 15=silent
```

### psg_voice_map

Keyed by `smpsPSGvoice` label name (e.g. `fTone_01`–`fTone_09`). When `smpsPSGvoice fTone_03` appears in channel data, the PSG channel switches to this instrument.

```yaml
psg_voice_map:
  fTone_01:
    mod_instrument: 8      # MOD instrument slot (1-based)
    type: tone
    root: A3               # MOD note anchor; determines target_rate
    synth_root: A3         # (optional) synthesis pitch override
    envelope: fTone_01     # Named envelope for amplitude shaping
    base_volume: 0
  fTone_03:
    mod_instrument: 9
    type: tone
    root: A3
    envelope: fTone_03
```

### PsgInstrumentEntry fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `mod_instrument` | int | yes | MOD slot (1-based, 1–31) |
| `type` | str | yes | `tone` / `white_noise` / `periodic_noise` |
| `root` | note | yes | MOD note anchor; controls `target_rate` AND where sample triggers |
| `synth_root` | note | no | Synthesis pitch override for tone entries (does NOT affect `target_rate`) |
| `noise_rate` | int | no | Noise divider: 0=N/512, 1=N/1024, 2=N/2048, 3=follow ch2 |
| `envelope` | str/list | no | Named table key (e.g. `fTone_04`) or inline list of per-frame attenuation deltas |
| `base_volume` | int | no | SN76489 base attenuation (0=max, 15=silent) |

---

## Pitch: root, synth_root, and target_rate for PSG Tones

The relationship between these three values is identical to YM2612 (see `docs/synthesis.md` §Pitch):

- **`root`** always determines `target_rate`:
  ```
  target_rate = round(amiga_clock / PERIOD_TABLE[root.value])
  ```
  This controls how fast the Amiga plays back the sample. It does NOT change because of `synth_root`.

- **`synth_root`** overrides the frequency used when rendering via the SN76489 emulator.
  The chip synthesizes at `synth_root`'s frequency, but the MOD sampler plays it at the `root` rate.
  Use this when the actual chip pitch differs from `root` due to transposition.

- For **noise entries**, `root` only sets the sample playback rate; SN76489 noise has no musical pitch.

### PSG frequency divider

```
N = round(clock_rate / (2 × freq × 16)),  clamped 1–1023
```

For MOD note index (0=C1, 12=C2, 24=C3, 33=A3):
```
freq = 440 × 2^((note_idx - 33) / 12)
```

`note_to_psg_n(mod_note_index, clock_rate)` in `sn76489/renderer.py` does this calculation.

### synth_note_idx

```
synth_note_idx = synth_root - 12    (if synth_root set)
synth_note_idx = root.value         (otherwise)
```

The −12 offset maps SMPS semitone convention to renderer index (idx 0 = C1).

**Missing synth_root on a transposed tone** → synthesizes at wrong octave → thin or
inaudible output. Set `synth_root` to the SMPS note the chip actually plays at.

---

## Envelope Tables

Nine Sonic 1 ROM envelope tables are defined in `configs/settings.yaml` under `psg_envelope_tables`:

```yaml
psg_envelope_tables:
  fTone_01: [0,0,0,1,1,1,2,2,2,3,3,3,4,4,4,5,5,5,6,6,6,7]
  fTone_02: [0,2,4,6,8,16]
  fTone_03: [0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7]
  fTone_04: [0,0,2,3,4,4,5,5,5,6]
  fTone_05: [0,0,0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,2,2,2,2,2,2,2,2,3,3,3,3,3,3,3,3,4]
  fTone_06: [3,3,3,2,2,2,2,1,1,1,0,0,0,0]
  fTone_07: [0,0,0,0,0,1,1,1,1,1,2,2,2,2,2,3,3,3,4,4,4,5,5,5,6,7]
  fTone_08: [0,0,0,0,0,1,1,1,1,1,2,2,2,2,2,2,3,3,3,3,3,4,4,4,4,4,5,5,5,5,5,6,6,6,6,6,7,7,7]
  fTone_09: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
```

- Each value is an **attenuation delta** added to `base_volume` per VBlank frame (60 Hz NTSC / 50 Hz PAL).
- `0` = no attenuation above base; higher = quieter.
- Last entry is held indefinitely (driver uses `$80` terminator; synthesizer clamps index at `len - 1`).
- The driver steps the envelope once per frame at `DurationTimeout` expiry.
- For envelope descriptions, see `docs/smps_driver.md` §PSG Channels.

### Inline envelope

Instead of a named key, you can pass a list directly:
```yaml
envelope: [0, 0, 2, 4, 6, 10, 15]
```

---

## Normalization

PSG uses hardware-max normalization to preserve the natural amplitude ratio of tone vs. noise:

```
scale = 127.0 / psg_output_max
```

- Tones peak at ±127 (int8 max), since the SN76489 emulator tone amplitude peaks at `psg_output_max` (default 4096).
- White noise peaks at ±64, because `sn76489.c` halves the noise channel output (`Channels[3] >>= 1` with boost_noise set), so it peaks at `psg_output_max / 2` = 2048.
- This 2:1 ratio mirrors the actual hardware balance on a Mega Drive.
- There is no per-sample normalize option; the relative tone:noise balance is always preserved.

---

## SN76489 Emulator Internals

**C source:** `reference/SN76489/sn76489.c` + `panning.c` (VGMPlay fork, Mega Drive config)

**Mega Drive configuration:**
```
FB_SEGAVDP  = 0x0009   (Sega VDP 16-bit LFSR feedback pattern)
SRW_SEGAVDP = 16       (shift register width)
boost_noise = 1        (doubles noise channel amplitude to match hardware)
```

**PSG volume table:** `PSGVolumeValues[16]` — 2 dB attenuation per step, index 0 = 4096 (max), index 15 = 0 (silence).

**Register protocol:**

Tone frequency (channels 0–2), 10-bit divider N:
```
Byte 1 (latch):  1 CC 0 NNNN   (low 4 bits of N)
Byte 2 (data):   0 0 NNNNNN    (high 6 bits, N >> 4)
where CC = channel index (0–2)
```

Volume (any channel 0–3):
```
Latch:  1 CC 1 VVVV   (0 = max, 15 = silent)
```

Noise (channel 3):
```
0xE0 | (fb << 2) | rate
where:
  fb   : 0 = periodic noise, 1 = white noise
  rate : 0/1/2 = N/512, N/1024, N/2048; 3 = follow tone ch2 LFSR clock
```

**Critical: `SN76489_Reset` must be called manually after `SN76489_Init`.** The C source has the reset call commented out inside `Init`. Without it, `Registers[]`, `ToneFreqVals[]`, and `IntermediatePos[]` contain garbage from `malloc`, causing out-of-bounds reads into `PSGVolumeValues`.

`SN76489_Update` writes stereo INT32 buffers; max amplitude ≈ ±4096 per tone channel.

---

## Module API Reference

### `sn76489/build.py`

- `get_lib_path() → Path` — Returns compiled library path, rebuilding from C sources if the DLL is missing or older than the sources.
- Compiles `reference/SN76489/sn76489.c` + `panning.c` using gcc or MSVC.
- Output: `sn76489/sn76489.dll` (Windows) or `sn76489/sn76489.so` (Unix).

### `sn76489/wrapper.py` — `SN76489` class

```python
SN76489(clock_rate=3_579_545, sample_rate=44100)
```

| Method | Description |
|--------|-------------|
| `write_tone_freq(ch, n)` | Set tone channel ch (0–2) frequency divider N (1–1023) |
| `write_volume(ch, vol)` | Set channel ch (0–3) volume; 0=max, 15=silent |
| `write_noise(white, rate)` | Configure noise: white=True/False, rate=0/1/2/3 |
| `render_samples(n) → list[(L,R)]` | Render n samples as (int32, int32) stereo pairs |
| `shutdown()` | Free chip context |

### `sn76489/renderer.py`

```python
note_to_psg_n(mod_note_index, clock_rate=_NTSC_CLOCK) → int
```
Converts MOD note index (0=C1, 24=C3) to 10-bit SN76489 divider N.

```python
render_psg_tone(mod_note_index, sustain_secs, release_secs, clock_rate, target_rate) → (bytes, int)
render_psg_tone_raw(..., envelope, base_volume, fps) → (list[int], int)
```
Render a PSG square-wave tone to 8-bit mono PCM (packed) or raw int list (before normalization).

```python
render_psg_noise(white, noise_rate, sustain_secs, release_secs, clock_rate, target_rate, tone2_n) → (bytes, int)
render_psg_noise_raw(..., envelope, base_volume, fps, tone2_n) → (list[int], int)
```
Render a PSG noise burst. `tone2_n` sets tone ch2 divider when `noise_rate=3` (follow ch2).

All render functions return `(pcm_or_list, sample_rate_hz)`.

### `sn76489/sample_generator.py`

```python
generate_psg_samples(config, psg_synth, verbose=False) → dict[int, tuple[bytes, int]]
```

Renders all `PsgInstrumentEntry` objects from `config.psg_map` and `config.psg_voice_map`.

Pipeline:
1. Iterate all entries; call `render_psg_tone_raw()` or `render_psg_noise_raw()` per entry.
2. Trim trailing silence from each raw list.
3. Global normalization pass: `scale = 127.0 / psg_synth.psg_output_max`.
4. Convert to int8 bytes.
5. Return `{inst_num: (pcm_bytes, sample_rate_hz)}`.

Returns a dict ready for insertion into a `ModFile` via `sample_list`.

---

## Common Mistakes

### Missing synth_root on a transposed tone

A PSG tone channel with `smpsChangeTransposition` plays the chip at a different pitch than the SMPS
byte label. If `synth_root` is not set, the emulator synthesizes at the `root` pitch (wrong octave),
producing a different timbre or silence.

**Fix:** Set `synth_root` to the SMPS note the chip actually plays at after transposition:
```
synth_root = low + total_transpose
```

### noise_rate: 3 (follow ch2) not modeled

`noise_rate: 3` makes the LFSR clock from PSG tone ch2's frequency divider. In the synthesizer,
`tone2_n=None` means the LFSR clocks at the emulator's reset default (N=1), not the hardware
rate-3 behaviour. Use `noise_rate: 0` as the closest fixed-rate approximation.

This is acceptable for Sonic 1 Title Screen where PSG tone ch2 is never explicitly tuned.
See `docs/limitations.txt` for details.

### Using deprecated psg_form_map key

YAML key `psg_form_map` is deprecated. Use `psg_map` instead. The old key still works but
produces a `DeprecationWarning`.

### psg_output_max mismatch

If `psg_output_max` does not match the actual peak amplitude from the emulator, the noise
channel will be too quiet or too loud relative to tones. The default value of 4096 matches
the `PSGVolumeValues[0]` entry in `sn76489.c`. Do not change it unless you modify the
C emulator's volume table.
