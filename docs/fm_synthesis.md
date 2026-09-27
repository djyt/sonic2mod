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
python convert.py configs/01_title_screen.yaml
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
fm_synthesis:
  enabled: true             # Master switch; false = samples loaded from samples_dir instead
  mode: ym2612              # "ym2612" (MD1/MD2 VA2) or "ym3438" (YM3438 accurate)
  clock_rate: 7670454       # Mega Drive NTSC YM2612 master clock (Hz)
  amiga_clock: 3546895      # PAL Amiga clock for MOD target_rate calculation
  sustain_duration: auto    # Seconds note held on before key-off, or auto (see § sustain_duration: auto)
  release_padding: 0.5      # Seconds captured after key-off (release tail)
  threads: normal           # Instruments rendered at once: normal (cores − 1), max (all cores), or a number
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Set `true` to generate samples; requires gcc/MSVC |
| `mode` | str | `"ym2612"` | `"ym2612"` = MD1/MD2 VA2 DAC behaviour (sign bias, ×3 level); `"ym3438"` = discrete YM3438. The renderer keeps the instance's mode across its per-note resets, and the batch helpers subtract the mode's own DC (72 / 0) so silence is 0 in both |
| `clock_rate` | int | `7670454` | Do not change for Sonic 1 |
| `amiga_clock` | int | `3546895` | PAL Amiga; use 3579545 for NTSC Amiga (rare) |
| `sustain_duration` | float or `auto` | `auto` (settings.yaml; `1.5` when the key is absent) | Seconds held before key-off. `auto` = the longest ring in the song, see below |
| `release_padding` | float | `0.5` | Longer = more release tail; affects sample file size |
| `threads` | str/int | `"normal"` | Render threads: `normal` = CPU cores − 1 (never below 1), `max` = all cores, or a count. Output is byte-identical whatever the value |

**Clock rates explained:**
- `clock_rate = 7670454` Hz → native synthesis rate = 7670454 / 6 / 24 ≈ **53,267 Hz**
- `amiga_clock = 3546895` Hz → `target_rate = amiga_clock / period` where period is from PERIOD_TABLE

### `sustain_duration: auto`

A synthesised sample does not loop: a note that outlasts its sample goes silent.  `auto`
makes the sustain long enough for the song.  `SmpsToModConverter._resolve_sustain` calls
`_sustain_needs`, which walks every enabled FM channel with the same `DriverState` as the
conversion and measures, per MOD instrument, the longest **ring** any of its notes needs:

- A ring is a note plus the `smpsNoAttack` continuations after it (no `C00` is written for
  those, so the sample keeps advancing).  A plain rest or the next note restarts the sample.
  One row is added for the row grid (`EDx` delays, cut placement).
- Its length is measured in the MOD's own time, summed over the tempo segments
  (`smpsSetTempoMod` changes the BPM), after `smpsSetTempoDiv` re-timing.
- It is measured at the sample's playback rate.  The sample is synthesised at the rate of
  the **first** entry naming the instrument (`_synthesis_roots`, the order
  `generate_fm_samples` walks the maps); a note played above that root runs the sample
  faster by root period / note period.  A positive `sample_list` finetune adds
  2^(finetune / 96).  The MOD note is the one the conversion triggers (range lookup in the
  config's `range_space`, `root + (key − low)`, or the channel transpose).

The FM sustain is the largest need over its instruments, capped at 10 s.  Independently,
`generate_fm_samples` caps each instrument's sustain to what a sample may hold at its rate
(`core.pcm.max_sustain_secs`: the `max_sample_kb` limit less the release).  `max_sample_kb`
is a top-level key of `settings.yaml`: `128` is the format's own limit (131070 bytes, a
16-bit word count, which Paula's length register shares and OpenMPT, the FT2 clone and
ProTracker 2.3E+/3.x play), `64` is the original ProTracker editor's (65534 bytes, its
four-hex-digit length field).  Where a note still outlasts its sample, `convert.py` prints a
`sustain_short` warning naming the instrument, the seconds needed and the limit that applies
(the setting, the 10 s cap, or the sample limit; the fix for the last is a lower `root`, which
halves the bytes per second per octave).  PSG works the same way (`docs/psg_synthesis.md`).
Stage Clear's PSG instrument 9 is the known case: its PSG2 range plays the PSG1 sample two
octaves up, so a 2.55 s note needs 13 s of it.  At `max_sample_kb: 64`, 17 instruments in
six songs (Marble Zone, Spring Yard, Scrap Brain, Robotnik, Final Zone, Credits) warn as well.

---

## Pitch: root, synth_root, and target_rate

This is the most critical (and confusing) part of synthesis configuration.

### Three pitch concepts

| Concept | Where set | Controls |
|---------|-----------|---------|
| `root` | `voice_map` entry | **The sample's base note**: the MOD note at which playback sounds at `synth_root`, and the note where `low` plays when `synth_root` is derived; also determines `target_rate` |
| `synth_root` | derived from the song; a `voice_map` entry may state it | **Rendering pitch** — the frequency the chip renders at: the chip pitch the instrument's notes play most often |
| `low` | `voice_map` entry | **Source range start**; the chip pitch it plays at is what MOD note `root` sounds |

### How they interact

**target_rate** (the Amiga playback rate) is always computed from `root`:
```python
target_rate = round(amiga_clock / PERIOD_TABLE[root.value])
```
`synth_root` does NOT affect `target_rate`.  A sample rendered at `synth_root` and played at
MOD note `m` therefore sounds at `synth_root + (m − root)` semitones.

**synth_root is derived.**  Before anything is rendered, `core.driver_state.resolve_synth_roots`
walks every channel with the `DriverState` and, for each rooted entry, finds D, the pitch the
chip really plays for the entry's `low` note (`low` plus the pitch offset and every
`smpsChangeTransposition`; for PSG through the driver's frequency table): with the sample at D
and the placement `m = root + (key − low)`, every note sounds at its chip pitch.  It then renders
higher than D where that serves the notes: envelopes run in real time on the chip but stretch
with playback rate in a MOD, so the sample is rendered at the chip pitch the instrument's notes
play most often (ties to the lower; at most an octave above D), and `synth_shift = synth_root − D`.
The shift never moves a note: the generator gives the sample a rate 2^(synth_shift/12) higher
than `root`'s playback rate, so MOD note `root` still sounds D, and every note keeps its place,
its playback rate and its bandwidth.  (Placing the notes lower instead, which the converter did
for one afternoon, halved the Chaos Emerald lead's playback rate to 4 kHz and lost everything
above 2 kHz; the hardware has a fifth of that lead's energy between 3 and 6 kHz.)  The cost is
the sample's size, 2^(synth_shift/12) times — hence the octave cap.  An instrument several
entries share (Credits folds voices onto 31 slots, Stage Clear's PSG2 sits two octaves up its
PSG1 sample) is rendered once, for the first entry that names it, and the pitch is chosen over
all of them; a later entry's `root` is already written so that it plays the first entry's sample
in tune.  A config never needs to state any of this; `convert.py` prints how many entries were
derived.

When an entry's `low` is played at several chip pitches (a voice used at two pitch offsets, or
under an `smpsChangeTransposition`, inside one source range) the most-played pitch is used and a
`synth_root_ambiguous` warning names the others: that entry needs `range_space: chip`
(`tools/config_to_chip_space.py`) or a split.

**Stating synth_root renders elsewhere in the range.**  A stated `synth_root` is the rendering
pitch, wherever in the range you want it — the middle, say, so the sample is stretched at most
half the range each way instead of a whole range upwards.  The entry's `synth_shift` is
`synth_root − D`, the sample's rate is raised by 2^(synth_shift/12), and the placement is the
usual `m = root + (key − low)`: the low note still plays at `root` and sounds D.

```yaml
voice_map:
  5:
    - low: G3          # chip pitches (range_space: chip); the range runs G3–B4
      high: B4
      mod_instrument: 7
      root: C2         # the note where G3 sounds
      synth_root: D4   # rendered at D4, the middle; the sample's rate is 2^(7/12) × C2's, notes stay put
```

**A `synth_root` name is a real pitch.**  `synth_root: A4` renders 440 Hz: `note_to_freq` gives the
standard frequency and `freq_to_fnum_block` uses the chip's formula
`fnum = f × 144 × 2^(21−block) / clock` (A4 → fnum 1083, block 4).  An SMPS FM note label is a
real pitch too — the driver's table puts `nC0` at 16.35 Hz — so the chip pitch of a source note is
simply `note + pitch_offset + smpsChangeTransposition`, and `tools/vgm_analyze.py` shows the same
names.  (Before 2026-09-18 both `freq_to_fnum_block` and the analyzer used `2^(20−block)`: the
synthesiser rendered an octave below the name and the analyzer read an octave high, so every
config carried `synth_root` values one octave above the rule.  All 103 were lowered when the
formulas were fixed.  On 2026-09-27 the 163 stated values were checked against the derivation:
162 matched and Spring Yard's `fTone_06` was an octave high — its four notes in the PSG table
were wrong until the derivation replaced it — and all were removed from the configs.)

**Why the rendering pitch matters:** FM timbre changes with pitch.  A bass voice rendered three
octaves too high has its modulation sidebands outside the audible range and comes out thin or
near-silent, which is what a missing transposition used to do before the derivation.
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

## Quantisation

Every sample is peak-normalised to its full 8 bits and quantised with TPDF dither and
first-order noise shaping (`core.pcm.to_int8`, the same treatment `sfx/amiga.py` gives the SFX
exports; the dither sequence is seeded from the sample's length, so a render is byte-identical
from run to run).  A decaying tail fades into a faint hiss instead of stepping through its last
few levels.

Balance between instruments is not the sample's job: the `sample_list` volume carries each
instrument's level, measured against the VGZ (`tools/vgm_compare.py --write-volumes`).  The
old global normalisation (`normalize_samples: false`, removed 2026-09-27; the key warns and is
ignored) scaled every instrument by the loudest one's peak, which cost quiet voices bits (Star
Light voice $00 kept 5.6 of them) and pinned loud single-carrier voices at volume 64 with
nowhere to go once a louder neighbour raised the scale.

---

## Carrier levels and the channel accumulator

Each instrument is rendered at the level most of its notes play at.  The converter's
`_plan_fm_render_levels` walks the song with the `DriverState` and finds, per MOD instrument,
the (TL offset, pan) most of its notes carry — the same choice `_plan_levels` makes for the
"baked" `sample_list` volume — and `program_voice` adds that TL offset to the carrier operators
exactly as the driver's `SetVoice` does (`add.b`, modulo 256; the chip keeps 7 bits).  The
sample therefore carries the level its `sample_list` volume stands for, and the `Cxx` law only
handles the notes that differ from it.  Modulators are never touched.

That matters because the chip sums a channel's carriers into a 9-bit accumulator
(`OPN2_ChGenerate` in Nuked: each operator's 14-bit output `>> 5`, the sum clamped to
−256…255, in both chip modes).  One carrier at TL 0 fills it exactly and can never clip.  Two or
more carriers at TL 0 overflow it — 55 Sonic 1 voices are built that way, every GHZ lead among
them — and how much they overflow depends on the channel's volume, which the driver adds to the
carriers before the sum.  Measured at C2, one second held, samples on the rail:

| Voice, channel | Hardware, at the channel's volume | Rendered at TL 0 |
|----------------|-----------------------------------|------------------|
| GHZ $02 on FM1 (+18 TL) | 0 % | 36 % |
| GHZ $02 on FM3 (+20 TL) | 0 % | 36 % |
| GHZ $04 on FM4 (+8 TL) | 0 % | 23 % |
| GHZ $02 on FM4 (+8 TL) | 6 % | 36 % |
| GHZ $03 on FM4 (+8 TL) | 34 % | 59 % |

So the level cannot be applied afterwards: scaling a TL-0 render down fixes the loudness but
keeps a distortion the hardware does not have, and the old `headroom_db` / `carrier_balance`
boosts (removed 2026-09-27; either key in `settings.yaml` warns and is ignored) did the opposite,
attenuating every carrier by a fixed 6 dB plus 20·log10(carriers) dB whatever the channel played
at, and throwing away 1–3 bits of resolution before the accumulator's shift.  Rendering at the
channel's own TL is what the hardware does, and clips exactly as much.

Flat-topped peaks on an algorithm 4–7 sample are therefore correct where the VGZ shows them
too.  The SFX renderer (`sfx/`) has always written TL this way (`fm_send_voice`).

---

## Synthesis Pipeline Detail

### Step 1 — OPN2 reset and voice programming

```python
opn2.reset()                 # keeps the instance's mode (OPN2(mode=settings.mode))
program_voice(opn2, voice, channel=0, tl_offset=track_volume)
```

`program_voice()` writes 30 YM2612 registers:
- 2 channel-level: `0xB0` (algorithm + feedback), `0xB4` (panning = L+R, AMS=0, PMS=0)
- 7 per-operator × 4 operators: DT/MUL, TL, KS/AR, AM/DR, SR, SL/RR, SSG-EG (always 0x00)

`tl_offset` is the track volume (`smpsHeaderFM` volume + `smpsAlterVol`) most of the instrument's
notes play at; it is added to the carriers' TL the way `SetVoice` does (§ Carrier levels).

### Step 2 — Frequency setup

```python
fnum, block = note_to_fnum_block(synth_note_idx)   # the driver's FM_FREQUENCIES entry
opn2.write_reg(0xA4 + ch, fnum_hi, bank)  # write high byte first (latches block+fnum[9:8])
opn2.write_reg(0xA0 + ch, fnum_lo, bank)  # write low byte (triggers frequency load)
```

The registers are the ones the Sonic 1 driver writes for that note (`core.driver_tables.
FM_FREQUENCIES`, index 1 = nC0, so MOD index i is table index i + 13): fnum 644–1216 with the
block from the octave.  That matters beyond pitch — rate scaling and detune read the key code
(block and the fnum's top bits), so A# and B written as fnum 574 one block up, as the old
frequency formula did, sounded the same pitch with a different envelope and detune; 73 of the
116 Sonic 1 voices use rate scaling and 77 detune.  Off the table, or at another clock,
`freq_to_fnum_block` (`fnum = freq × 144 × 2^(20−block) / clock_rate`) stands in.

### Step 3 — Key-on → render → key-off

```python
opn2.key_on(channel)                    # all 4 operators, reg 0x28
mono  = opn2.render_mono(ceil(native_rate * sustain_secs))    # array('i')
opn2.key_off(channel)                   # releases note
mono += opn2.render_mono(ceil(native_rate * release_secs))
```

The chip is clocked 24 times per output sample, all 24 `mol`/`mor` values are accumulated and
DC = 72 (24 × 3) subtracted to zero-centre the result.  `render_samples(n)` returns that as
`[(L,R), ...]`; `render_mono(n)` folds each pair to `(L + R) // 2` in C (`OPN2_RenderBatchMono`
in `ym3438_batch.c`) and returns an `array('i')`.  The note renderer uses `render_mono`: building
400 000 tuples per sample used to cost a fifth of the emulation time itself.  The SFX driver
(`sfx/render.py`) still uses the stereo call — it mixes L and R separately.

### Step 4 — Optional resample

```python
if target_rate != native_rate:
    mono = _resample(mono, native_rate, target_rate)   # core.resample (polyphase windowed sinc)
```

`_resample` is the polyphase Kaiser-windowed sinc the SFX renderer uses (`core/resample.py`;
32 taps, 512 phases, >70 dB stopband), rounded back to ints.  It replaced a box average, which
rolled off 3.9 dB at the target's Nyquist and left aliases only ~6 dB down, with window-length
jitter on non-integer ratios.

### Step 5 — Normalization and int8 packing

- `render_note()`: normalises the sample to ±127 and quantises it (dithered) before returning `bytes`.
- `render_note_raw()`: returns the raw mono `array('i')`.
- `generate_fm_samples()`: uses `render_note_raw()` for all instruments, then normalises and
  quantises each to its full 8 bits in a second pass (§ Quantisation).

### Concurrency

`generate_fm_samples()` first decides every instrument's job (voice, synth pitch, target rate),
then renders the jobs on a thread pool, one instrument per thread, `threads` at a time
(`normal` = cores − 1, `max`, or a number).  ctypes releases the GIL for the batch call and Nuked-OPN2
keeps all chip state in the per-instance struct, so the renders run truly in parallel; each
worker thread owns its own `OPN2`.  The emulator's one global is the chip-type flag, which every
reset writes with the same value.  Results are consumed in job order, so the MOD is
byte-identical whatever the thread count.  GHZ (11 instruments) converts in about 1 s instead
of 4.5 s; Credits (25 instruments, 10 s sustain) in about 2 s instead of 12.5 s.

Final encoding: `core.pcm.to_int8` — TPDF dither, first-order noise shaping, clamp, int8 stored as uint8.

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
OPN2(mode="ym2612")         # builds DLL, resets chip; the mode is remembered on the instance
opn2.reset(mode=None)       # full chip reset; None keeps the instance's mode, a string switches it
opn2.write_reg(addr, data, bank=0)  # write YM register with flush
opn2.key_on(channel, operators=0xF) # trigger key-on
opn2.key_off(channel)                # release all operators
opn2.render_samples(n) → list[tuple[int,int]]  # n stereo pairs (L,R)
opn2.render_mono(n) → array('i')                 # the same n samples as (L + R) // 2, folded in C
OPN2.NATIVE_RATE            # ≈ 53,267 Hz (class attribute)
```

### `ym2612/voice.py` — program_voice

```python
program_voice(opn2, voice, channel, tl_offset=0)
```

Writes 30 YM2612 registers for the given `SmpsVoice`, `tl_offset` (the track volume) added to
the carriers' TL as `SetVoice` does. Does NOT set frequency or key-on.

### `ym2612/renderer.py` — render functions

```python
render_note(voice, mod_note_index,
            sustain_secs=1.5, release_secs=0.5,
            target_rate=None, opn2=None, channel=0,
            clock_rate=7670454, tl_offset=0)
    → (bytes, int)   # 8-bit signed PCM, sample_rate_hz

render_note_raw(voice, mod_note_index, ...)
    → (array('i'), int)  # pre-normalized mono, sample_rate_hz

note_to_fnum_block(mod_note_index, clock_rate=7670454) → (int, int)
    # (fnum, block) the driver writes for the note (FM_FREQUENCIES); the formula off the table

note_to_freq(mod_note_index) → float
    # 440 × 2^((idx-45)/12); idx 0=C1, 35=B3, 45=A4(440Hz)

freq_to_fnum_block(freq, clock_rate=7670454) → (int, int)
    # (fnum, block) from a frequency; targets fnum in [512, 1023]
```

`render_note` always resets the OPN2 internally at the start of each call.

### `ym2612/sample_generator.py` — generate_fm_samples

```python
generate_fm_samples(song, config, synth, tl_offsets=None) → dict[int, tuple[bytes, int]]
# Returns {mod_instrument_number: (pcm_bytes, target_rate_hz)}, each peak-normalised
# tl_offsets: {instrument: track volume} to render at (the converter's _plan_fm_render_levels)
# Only voice_map entries with entry.root set are included.
# Renders on a thread pool, one instrument per thread, synth.worker_threads() at a time (the `threads` setting).
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

**Cause:** Wrong `SMPS_OP_TO_REG_OFFSET` in `core/driver_tables.py` — OP1 (TL≈$01, near max volume)
placed in the self-feedback slot.
→ Verify `SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)` in `core/driver_tables.py`. Do NOT change it.

### Thin/bright sound on bass voices

**Cause:** `synth_root` too high — synthesizing at the SMPS byte pitch (e.g. C5) when the
chip plays at C2 due to smpsChangeTransposition. FM algorithm produces different timbres at
different octaves; bass voices synthesized at C5 lose their low-frequency character.
→ Set `synth_root` to the actual chip pitch (typically `low + total_transpose`).

### Clipping (flat waveform peaks)

**Cause:** Algorithm 4/5/6/7 with two or more carriers at TL 0 overflows the chip's 9-bit channel
accumulator.  That is hardware behaviour, not a synthesis fault: the VGZ has the same waveform
(§ Carrier levels and the channel accumulator).  A single-carrier voice cannot clip in the sample.

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

**Cause:** every sample is peak-normalised, so the balance lives entirely in the `sample_list`
volumes, and a hand-written one is a guess.
→ Measure them: `python tools/vgm_compare.py <config> <vgz> --write-volumes`, re-convert, re-run.
