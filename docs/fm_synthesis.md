# YM2612 FM Synthesis Pipeline

How sonic2mod generates MOD sample data from Sonic 1 FM voice patches using Nuked-OPN2.

Related docs: `docs/smps_driver.md` (SMPS voice format), `docs/pipeline.md` (conversion pipeline),
`docs/yaml_config.md` (voice_map schema).

---

## Overview

When `synthesis.enabled: true`, sonic2mod renders each FM voice patch as 8-bit PCM and embeds
the result directly into the MOD file as a sample. The Amiga then replays the PCM at the correct
pitch via its period table — no FM chip needed at playback time.

The pipeline:

```
voice_map entry  →  render_note_raw()  →  OPN2 emulation  →  PCM mono  →  normalize  →  MOD sample
(voice + root)       (synth pitch)        (Nuked-OPN2 DLL)   (resample)   (int8)
```

All five pipeline segments must be complete before synthesis produces usable output:

| Segment | File | Purpose |
|---------|------|---------|
| 1 | `ym2612/build.py` + `ym2612/wrapper.py` | Compile ym3438.c → DLL; ctypes OPN2 class |
| 2 | `ym2612/voice.py` | SmpsVoice → YM2612 register writes |
| 3 | `ym2612/renderer.py` | Voice + pitch → 8-bit PCM |
| 4 | `ym2612/sample_generator.py` | voice_map → `{inst: (pcm, rate)}` dict |
| 5 | `smps2mod.py` + `convert.py` | Install synthesized samples into ModFile |

---

## Quick Start

### 1. Enable synthesis

```yaml
# configs/settings.yaml
synthesis:
  enabled: true
```

### 2. Run smoke tests (verify DLL + pipeline)

```bash
python ym2612/validate.py      # segment 1: A4 tone → output/validate_test.raw
python ym2612/voice.py         # segment 2: voice 0, 100 ms → prints peak
python ym2612/renderer.py      # segment 3: voice 1 at A3 → output/renderer_test.raw
python ym2612/sample_generator.py  # segment 4: voice 1 at A3 → output/sample_gen_test.raw
```

### 3. Build and convert

```bash
python convert.py configs/title_screen.yaml
```

Synthesis runs automatically when `synthesis.enabled: true` and the DLL is compiled.
Each voice_map entry with a `root` produces one synthesized MOD sample.

### 4. Auditing in Audacity

Load `.raw` smoke test files in Audacity:
- **File > Import > Raw Data**
- Encoding: **Signed 16-bit PCM**, byte order: **Little-endian**, channels: **1 (Mono)**
- Sample rate: printed by each smoke test (typically 53,267 Hz or the target_rate value)

---

## Settings Reference (`configs/settings.yaml`)

```yaml
synthesis:
  enabled: false            # Master switch; false = silent placeholder samples
  mode: ym2612              # "ym2612" (MD1/MD2 VA2) or "ym3438" (YM3438 accurate)
  clock_rate: 7670454       # Mega Drive NTSC YM2612 master clock (Hz)
  amiga_clock: 3546895      # PAL Amiga clock for MOD target_rate calculation
  sustain_duration: 1.5     # Seconds note held on before key-off
  release_padding: 0.5      # Seconds captured after key-off (release tail)
  normalize_samples: false  # true = per-sample peak normalization; false = global (preserves balance)
  headroom_db: 6.0          # Carrier TL boost to prevent DAC clipping; 6 dB ≈ 8 TL steps
  carrier_balance: true     # Scale boost by carrier count (prevents multi-carrier over-attenuation)
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Set `true` to generate samples; requires gcc/MSVC |
| `mode` | str | `"ym2612"` | `"ym2612"` = slightly warmer; `"ym3438"` = bit-exact |
| `clock_rate` | int | `7670454` | Do not change for Sonic 1 |
| `amiga_clock` | int | `3546895` | PAL Amiga; use 3579545 for NTSC Amiga (rare) |
| `sustain_duration` | float | `1.5` | Longer = more of the sustain envelope captured |
| `release_padding` | float | `0.5` | Longer = more release tail; affects sample file size |
| `normalize_samples` | bool | `false` | See Normalization section below |
| `headroom_db` | float | `6.0` | See Headroom section below |
| `carrier_balance` | bool | `true` | See Headroom section below |

**Clock rates explained:**
- `clock_rate = 7670454` Hz → native synthesis rate = 7670454 / 6 / 24 ≈ **53,267 Hz**
- `amiga_clock = 3546895` Hz → `target_rate = amiga_clock / period` where period is from PERIOD_TABLE

---

## Pitch: root, synth_root, and target_rate

This is the most critical (and confusing) part of synthesis configuration.

### Three pitch concepts

| Concept | Where set | Controls |
|---------|-----------|---------|
| `root` | `voice_map` entry | **MOD note placement** — where the sample is triggered in the pattern; also determines `target_rate` |
| `synth_root` | `voice_map` entry (optional) | **Synthesis pitch** — the frequency the chip renders at |
| `low` | `voice_map` entry | **Source range start** and **default synthesis pitch** when `synth_root` is absent |

### How they interact

**target_rate** (the Amiga playback rate) is always computed from `root`:
```python
target_rate = round(amiga_clock / PERIOD_TABLE[root.value])
```
`synth_root` does NOT affect `target_rate`. This is intentional.

**synth_note_idx** (the note the OPN2 chip renders at) is:
```python
if entry.synth_root is not None:
    synth_note_idx = entry.synth_root - 12   # SMPS semitone → renderer index
else:
    synth_note_idx = entry.low - 12           # default: synthesize at 'low'
```
The `-12` offset exists because SMPS semitone 0 = C0, but the renderer's index 0 = C1 (one octave higher).

**What the Amiga hears:**
When the Amiga plays the sample at its period (derived from `root`), the output pitch is the
frequency of `synth_root` (or `low` if no `synth_root`). This works correctly when:

```
synth_root matches the actual chip pitch for that channel and voice switch
```

### When to use synth_root

**Case 1 — No smpsChangeTransposition:** `synth_root` is not needed. Synthesize at `low`.

```yaml
voice_map:
  5:
    - low: C4
      high: B5
      mod_instrument: 7
      root: C2
      # synth_root omitted: synthesize at C4
```

**Case 2 — smpsChangeTransposition shifts the chip pitch:** The SMPS byte says C5 but the chip
actually plays at C2 (because total_transpose = -36). Set `synth_root: C2` so the OPN2 renders
at C2's frequency — matching what the game plays.

```yaml
voice_map:
  8:
    - low: C5
      high: B6
      mod_instrument: 22
      root: C2
      synth_root: C2    # chip pitch = C2 due to total_transpose -36
```

Without `synth_root`, synthesis would render at C5 (523 Hz), which is three octaves too high.
FM timbre changes significantly with pitch — at C5 the modulation sidebands are outside the
audible range for a bass voice, producing a thin or near-silent result.

**Computing synth_root:**
```
synth_root = low + total_transpose − chan_cfg.transpose
```
where:
- `low` = SMPS source note (the `low` field)
- `total_transpose` = header pitch_offset + accumulated smpsChangeTransposition
- `chan_cfg.transpose` = channel's `transpose:` in YAML (which is already included in total_transpose
  at conversion time, so subtract it to get the runtime delta from smpsChangeTransposition only)

Example — GHZ voice $08 on FM3:
- `low = C5` (SMPS semitone 60)
- Header pitch_offset for FM3 = $F4 = -12
- smpsChangeTransposition $E8 = -24 (cumulative before voice switch)
- `total_transpose = -12 + (-24) = -36`
- YAML `transpose: 0` (root handles placement) so `chan_cfg.transpose = 0`
- `synth_root = 60 + (-36) - 0 = 24 = C2`

### root placement quality

A higher `root` value gives a higher `target_rate`, which means the Amiga sample is played back
at a faster rate with more audio frequency resolution. Higher quality:

```
target_rate = amiga_clock / PERIOD_TABLE[root.value]
# C1 (idx=0):  3546895 / 856 ≈  4144 Hz  (very low quality)
# C2 (idx=12): 3546895 / 428 ≈  8287 Hz  (acceptable)
# C3 (idx=24): 3546895 / 214 ≈ 16574 Hz  (good)
# B3 (idx=35): 3546895 / 113 ≈ 31389 Hz  (near CD quality)
```

Choose the highest `root` whose full range `root + (high − low)` stays within C1–B3 (indices 0–35).

---

## Normalization

### Global normalization (recommended, `normalize_samples: false`)

All instruments are scaled by the **same factor** derived from the loudest sample across the entire
set. This preserves the **relative volume balance** between voices — a quiet pad stays quieter than
a loud lead, matching the original chip output.

The global scale factor is printed during conversion:
```
  Global peak: 8421  (scale=0.0151)
```

### Per-sample normalization (`normalize_samples: true`)

Each instrument is independently peak-normalized to ±127. Use when:
- You want every sample at maximum volume (e.g. for hand-editing in a tracker)
- A few very loud voices are drowning others at global scale
- You plan to set MOD sample volumes manually

**Downside:** Quiet voices (e.g. algorithm 4 with TL=0 on both carriers) are boosted to the same
level as loud voices, losing the authentic balance.

---

## Headroom and Carrier Balance (Anti-Clipping)

### The problem

YM2612 voices with algorithm 4, 5, 6, or 7 have multiple carrier operators. When carrier TL = 0
(maximum volume), their outputs sum in the OPN2's 9-bit internal DAC and clip before any Python
normalization can correct it. The resulting samples have hard-clipped waveforms.

### The fix: carrier TL boost

`voice.py:program_voice()` adds a TL attenuation boost to carrier operators before writing to the
OPN2 emulator. Modulators are unaffected (timbre preserved).

**`headroom_db`:** Base boost for every carrier, regardless of algorithm. Converts to TL steps:
```python
headroom_tl = round(headroom_db / 0.75)
# 6.0 dB → 8 TL steps   (default)
# 3.0 dB → 4 TL steps
# 12.0 dB → 16 TL steps
```
Each TL step = 0.75 dB attenuation.

**`carrier_balance`:** Additional boost proportional to carrier count, to compensate for the fact
that 3 carriers sum to 3× the amplitude of 1 carrier:
```python
balance_tl = round(20 * math.log10(N_carriers) / 0.75)
# 1 carrier: +0   (Alg 0–3)
# 2 carriers: +8  (Alg 4)    — extra 6 dB
# 3 carriers: +13 (Alg 5/6)  — extra 9.5 dB
# 4 carriers: +16 (Alg 7)    — extra 12 dB
```

**Total carrier boost at defaults:**
| Algorithm | Carriers | headroom | balance | Total boost |
|-----------|----------|----------|---------|-------------|
| 0–3 | 1 | +8 TL | +0 | **+8 TL** |
| 4 | 2 | +8 TL | +8 | **+16 TL** |
| 5, 6 | 3 | +8 TL | +13 | **+21 TL** |
| 7 | 4 | +8 TL | +16 | **+24 TL** |

Maximum TL is clamped to 127 (`min(127, eff_tl + total_boost)`).

**Tuning headroom_db:** If synthesized samples still clip (waveform flattens at peaks in Audacity),
increase `headroom_db`. If samples are too quiet relative to DAC drums, decrease it. The default
6.0 dB is a reasonable starting point for most Sonic 1 voices.

---

## Synthesis Pipeline Detail

### Step 1 — OPN2 reset and voice programming

```python
opn2.reset(mode)
program_voice(opn2, voice, channel=0, headroom_tl=..., carrier_balance=...)
```

`program_voice()` writes 30 YM2612 registers:
- 2 channel-level: `0xB0` (algorithm + feedback), `0xB4` (panning = L+R, AMS=0, PMS=0)
- 7 per-operator × 4 operators: DT/MUL, TL, KS/AR, AM/DR, SR, SL/RR, SSG-EG (always 0x00)

### Step 2 — Frequency setup

```python
freq = note_to_freq(synth_note_idx)        # 440 × 2^((idx-45)/12)
fnum, block = freq_to_fnum_block(freq)     # targets fnum in [512, 1023]
opn2.write_reg(0xA4 + ch, fnum_hi, bank)  # write high byte first (latches block+fnum[9:8])
opn2.write_reg(0xA0 + ch, fnum_lo, bank)  # write low byte (triggers frequency load)
```

`freq_to_fnum_block` formula: `fnum = freq × 144 × 2^(20−block) / clock_rate`

### Step 3 — Key-on → render → key-off

```python
opn2.key_on(channel)                    # all 4 operators, reg 0x28
sustain_samples = opn2.render_samples(int(native_rate * sustain_secs))
opn2.key_off(channel)                   # releases note
release_samples = opn2.render_samples(int(native_rate * release_secs))
```

`render_samples(n)` clocks the OPN2 24 times per output sample, accumulating all 24 `mol`/`mor`
values, then subtracting DC = 72 (24 × 3) to zero-centre the result. Returns `[(L,R), ...]`.

### Step 4 — Stereo → mono → optional resample

```python
mono = [(l + r) // 2 for l, r in samples]
if target_rate != native_rate:
    mono = _resample(mono, native_rate, target_rate)
```

`_resample` is a simple box-filter (integer average of input samples per window).
No external libraries required. Only downsampling is supported.

### Step 5 — Normalization and int8 packing

- `render_note()`: normalizes each sample individually before returning `bytes`.
- `render_note_raw()`: returns the raw `list[int]` for batch global normalization.
- `generate_fm_samples()`: uses `render_note_raw()` for all instruments, then applies global
  or per-sample normalization in a second pass.

Final encoding: `(max(-128, min(127, round(v * scale))) & 0xFF)` — int8 stored as uint8.

---

## OPN2 Emulator Internals

### Architecture (Nuked-OPN2, `reference/Nuked-OPN2/ym3438.c`)

Nuked-OPN2 is a cycle-accurate YM2612/YM3438 emulator by nukeykt. The `OPN2_Clock()` function
advances the chip by one master clock cycle.

**24-clock period (one audio sample):**

In YM2612 mode (`OPN2_SetChipType(0x01)`), the chip time-multiplexes 6 FM channels across 24
internal clocks. At the 6 output-enable clocks where `(cycles & 3) == 3`, the `mol`/`mor` outputs
contain `audio × 3`. At all other 18 clocks they carry `sign × 3` (≈ ±3 DC bias, not audio).

`render_samples()` **accumulates all 24 values per output sample** and subtracts DC = 72 (24 × 3):
```python
dc = 24 * 3   # = 72
l_sum, r_sum = 0, 0
for _ in range(24):
    OPN2_Clock(chip, buf)
    l_sum += buf[0]; r_sum += buf[1]
out.append((l_sum - dc, r_sum - dc))
```
Taking only the final clock value captures DC bias, not audio. This was the original bug.

### Register write protocol

Each `write_reg(addr, data, bank)` call:
1. `OPN2_Write(chip, bank*2,   addr)` — address latch port
2. Clock 24× (pipeline flush)
3. `OPN2_Write(chip, bank*2+1, data)` — data write port
4. Clock 24× (pipeline flush)

48 clocks total per register write = 2 audio samples of warmup. With 30 registers, voice
programming consumes ~60 audio samples before the key-on.

### Key-on register (0x28)

```
bits[6:4] = operator enable mask (OP4 OP3 OP2 OP1)
bits[2:0] = channel bits (ch 0-2 → 0-2; ch 3-5 → 4-6)
```

`key_on(channel)` sends mask `0xF` (all operators). `key_off(channel)` sends mask `0x0`.

### Bank mapping

```
Bank 0 (port 0/1): channels 0, 1, 2  → ch_in_bank = channel % 3
Bank 1 (port 2/3): channels 3, 4, 5  → ch_in_bank = (channel - 3) % 3
```

---

## Module API Reference

### `ym2612/wrapper.py` — OPN2 class

```python
OPN2(mode="ym2612")         # builds DLL, resets chip
opn2.reset(mode="ym2612")   # full chip reset + set chip type
opn2.write_reg(addr, data, bank=0)  # write YM register with flush
opn2.key_on(channel, operators=0xF) # trigger key-on
opn2.key_off(channel)                # release all operators
opn2.render_samples(n) → list[tuple[int,int]]  # n stereo pairs (L,R)
OPN2.NATIVE_RATE            # ≈ 53,267 Hz (class attribute)
```

### `ym2612/voice.py` — program_voice

```python
program_voice(opn2, voice, channel, headroom_tl=0, carrier_balance=False)
```

Writes 30 YM2612 registers for the given `SmpsVoice`. Does NOT set frequency or key-on.

### `ym2612/renderer.py` — render functions

```python
render_note(voice, mod_note_index,
            sustain_secs=1.5, release_secs=0.5,
            target_rate=None, opn2=None, channel=0,
            clock_rate=7670454,
            headroom_tl=0, carrier_balance=False)
    → (bytes, int)   # 8-bit signed PCM, sample_rate_hz

render_note_raw(voice, mod_note_index, ...)
    → (list[int], int)   # pre-normalized mono, sample_rate_hz

note_to_freq(mod_note_index) → float
    # 440 × 2^((idx-45)/12); idx 0=C1, 35=B3, 45=A4(440Hz)

freq_to_fnum_block(freq, clock_rate=7670454) → (int, int)
    # (fnum, block); targets fnum in [512, 1023]
```

`render_note` always resets the OPN2 internally at the start of each call.

### `ym2612/sample_generator.py` — generate_fm_samples

```python
generate_fm_samples(song, config, synth) → dict[int, tuple[bytes, int]]
# Returns {mod_instrument_number: (pcm_bytes, target_rate_hz)}
# Only voice_map entries with entry.root set are included.
```

Processing order:
1. `voice_map` entries with `root` (primary synthesis path)
2. `channel_instrument_map` entries with `root`
3. `legacy_voice_map` entries (deprecated; synthesized at C5/C1, emits DeprecationWarning)
4. Rootless `channel_instrument_map` entries (synthesized at C5/C1)

Already-synthesized instrument numbers (by `mod_instrument` value) are skipped to prevent
overwriting. First entry wins if two ranges share `mod_instrument`.

---

## Common Mistakes

### Silence or near-silence output

**Cause A:** `synth_root` not set on a transposed channel — synthesizing 2–3 octaves above
the actual chip pitch. FM sideband frequencies fall outside the audible range.
→ Compute `synth_root` as `low + total_transpose − chan_cfg.transpose`. See §synth_root above.

**Cause B:** `synthesis.enabled: false` in `configs/settings.yaml`.
→ Set `enabled: true`.

**Cause C:** DLL not compiled (`ym2612/ym3438.dll` missing).
→ Run `python ym2612/build.py` or call `OPN2()` which auto-builds.

### Distorted "overdriven guitar" sound

**Cause:** Wrong `_SMPS_OP_TO_REG_OFFSET` in `voice.py` — OP1 (TL≈$01, near max volume)
placed in the self-feedback slot.
→ Verify `_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)`. Do NOT change it.

### Thin/bright sound on bass voices

**Cause:** `synth_root` too high — synthesizing at the SMPS byte pitch (e.g. C5) when the
chip plays at C2 due to smpsChangeTransposition. FM algorithm produces different timbres at
different octaves; bass voices synthesized at C5 lose their low-frequency character.
→ Set `synth_root` to the actual chip pitch (typically `low + total_transpose`).

### Clipping (flat waveform peaks)

**Cause:** Algorithm 4/5/6/7 with TL=0 carriers; multiple carriers summing to saturation
inside OPN2 before Python normalization.
→ Increase `headroom_db` (try 9.0 or 12.0). Enable `carrier_balance: true`.

### Wrong pitch in MOD

**Cause A:** `root` set incorrectly — `low` is not anchored to the right MOD note.
→ Verify: source `low` note should play at `root`. Output = root + (source − low).

**Cause B:** `synth_root` mismatch — sample sounds at a different pitch than expected.
→ `synth_root` controls what frequency the sample sounds at; `root` controls where it is
triggered in the MOD pattern. Both must agree for pitch to be correct.

### Instruments silently skipped

**Cause:** Two voice_map entries share the same `mod_instrument` value. The first one rendered
wins; subsequent entries for the same slot are skipped.
→ Assign unique `mod_instrument` values for each range that needs a distinct sample.

### Relative volumes inconsistent between instruments

**Cause:** `normalize_samples: true` — per-sample normalization maximizes each instrument
independently, erasing the relative balance.
→ Use `normalize_samples: false` (global normalization preserves balance).
