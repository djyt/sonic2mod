# YAML Configuration Reference

Per-song YAML configs allow fine-grained control over the SMPS-to-MOD conversion. Place them in the `configs/` directory.

## Usage

```bash
python convert.py configs/my_song.yaml
```

CLI `--output` overrides the YAML `output_file` if both are given.

## Full Schema

```yaml
# Song name written into MOD header (max 19 characters)
name: "Title Screen"

# File paths
input_file: "path/to/song.asm"
output_file: "output/title_screen.mod"

# Timing
target_bpm: 150        # BPM (32–255), written as Fxx effect on row 0
target_speed: 6        # Ticks per row (1–31), ProTracker default is 6
ticks_per_row: 6       # SMPS ticks per MOD row (controls time scaling)
auto_bpm: true         # Derive BPM from SMPS tempo header (overrides target_bpm)
region: ntsc           # Console region: "ntsc" (60 Hz) or "pal" (50 Hz)

# MOD format
num_mod_channels: 10   # Channel count: 4, 8, 10, 12, 14, or 16
max_patterns: 127      # Truncation limit (max 127)

# Channel mappings (one entry per SMPS channel to convert)
channels:
  - source: DAC        # SMPS source: DAC, FM1–FM5, PSG1–PSG3
    mod_channel: 0     # 0-based MOD channel index
    instrument: 1      # MOD instrument number (1–31)
    transpose: 0       # Semitone offset (default 0)
    volume: 64         # MOD volume (0–64, default 64)
    enabled: true      # Set false to skip this channel

  - source: FM1
    mod_channel: 1
    transpose: -36     # Shift SMPS octave 3+ into MOD range
    instrument: 2

  - source: FM2
    mod_channel: 2
    transpose: -24     # Lower channels may need less shift
    instrument: 3

  # ... more channels ...

# DAC sample mappings (how SMPS drum names become MOD notes)
# Choose mod_note to match the sample's native playback rate on Amiga hardware.
# See "DAC Sample Rates" section below for pitch-correct note values.
dac_samples:
  - name: dKick              # SMPS DAC name
    mod_instrument: 1        # MOD instrument number to trigger
    mod_note: C2             # Pitch-correct note for 8,250 Hz sample

  - name: dSnare
    mod_instrument: 2
    mod_note: Fs3            # Pitch-correct note for 24,000 Hz sample

  - name: dTimpani
    mod_instrument: 3
    mod_note: As1            # Pitch-correct note for 7,375 Hz sample

  # Timpani variants share instrument 3, using different notes for pitch
  - name: dHiTimpani
    mod_instrument: 3
    mod_note: Ds2            # 7,375 Hz × 1.30 = 9,588 Hz

  - name: dMidTimpani
    mod_instrument: 3
    mod_note: Cs2            # 7,375 Hz × 1.20 = 8,850 Hz

  - name: dLowTimpani
    mod_instrument: 3
    mod_note: A1             # 7,375 Hz × 0.97 = 7,154 Hz

  - name: dVLowTimpani
    mod_instrument: 3
    mod_note: A1             # 7,375 Hz × 0.95 = 7,006 Hz

# Optional: load real sample files instead of placeholders
# Source WAVs are in sonic_1/dac/dpcm/; convert to raw signed PCM:
#   sox kick.wav -t raw -r 8250 -e signed -b 8 -c 1 kick.raw
sample_list:
  - [1, "kick.raw", 64, 0]      # [inst_num, filename, volume, finetune]
  - [2, "snare.raw", 64, 0]
  - [3, "timpani.raw", 64, 0]

samples_dir: "./samples/"       # Base path for sample files
```

## Channel Source Names

| Source | SMPS Channel | Typical Content |
|--------|--------------|-----------------|
| `DAC` | DAC | Drum/percussion samples |
| `FM1` | 1st FM channel | Lead melody |
| `FM2` | 2nd FM channel | Harmony/bass |
| `FM3` | 3rd FM channel | Counter-melody |
| `FM4` | 4th FM channel | Pads/chords |
| `FM5` | 5th FM channel | Often shares FM1 data with offset |
| `PSG1` | 1st PSG channel | High melody/arpeggios |
| `PSG2` | 2nd PSG channel | Harmony |
| `PSG3` | 3rd PSG channel | Noise/rhythm |

## Transpose Guide

SMPS uses 8 octaves (C0–B7, bytes $81–$DF). MOD has 3 octaves (C1–B3, 36 semitones). You must transpose to fit.

| SMPS Note Range | Transpose | MOD Result |
|-----------------|-----------|------------|
| C0–B2 (low) | -0 to -12 | C1–B3 (may still clip high) |
| C3–B5 (mid) | **-36** | C1–B3 (recommended for most FM) |
| C4–B6 (high) | -48 | C1–B3 (may clip low) |
| Mixed range | per-channel | Tune each channel separately |

Notes outside C1–B3 after transpose are clamped with a warning. Use per-channel transpose in YAML for best results.

## voice_map

Routes a SMPS voice index + source-note range to a specific MOD instrument slot, with an optional pitch anchor (`root`) and optional synthesis-pitch override (`synth_root`).

For synthesis pitch matching (how `root`, `synth_root`, and `low` interact with `target_rate`), see `docs/synthesis.md` §Pitch.

```yaml
voice_map:
  0:                          # SMPS voice index (from smpsSetvoice)
    - low:  G5                # Bottom of source-note range (inclusive)
      high: G6                # Top of source-note range (inclusive)
      mod_instrument: 4       # MOD instrument slot (1-based)
      root: F2s               # G5 plays at F#2; each semitone above shifts output by 1
      synth_root: C6          # (optional) synthesize at C6; output pitch = C6 frequency
    - low:  Gs6
      high: C7
      mod_instrument: 12
      root: G3
```

`low`/`high` are SMPS note names (no `n` prefix): `C5`, `Gs6`, `B7`, etc. Range lookup uses `(note_value − $81)`. `smpsAlterNote` is a raw FNUM offset (~10 cents) and does NOT affect range selection or pitch placement.

### root — pitch anchor

`root` is an **absolute** MOD note anchor. Source `low` always plays at `root`, unconditionally — smpsAlterPitch events and header pitch_offset do not affect this. The placement formula is:

```
output = root + (source − low)
```

Each semitone above `low` shifts the output up by 1, clamped to C1–B3.

Without `root`, the entry still selects the correct `mod_instrument` but pitch falls through to the channel-transpose path (`smps_note + total_transpose`).

### synth_root — synthesis pitch override

When synthesis is enabled, each `voice_map` entry is synthesized at `low` by default. For channels that use smpsAlterPitch, `low` is the SMPS byte value but the chip actually plays at a lower pitch (after transposition). `synth_root` lets you specify the pitch the chip actually synthesizes at.

`synth_root` sets a different SMPS semitone to synthesize at (same note-name syntax as `low`/`high`). `target_rate` is **not** adjusted — the output pitch equals `synth_root`'s frequency.

```
target_rate = amiga_clock / PERIOD_TABLE[root]   # unchanged by synth_root
synth_idx   = synth_root − 12                    # synthesis at synth_root's frequency
```

**Example** — GHZ voice $08, FM3, source C5–B6, total_transpose −36 (pitch_offset −12 + smpsAlterPitch −24). The chip plays at C5 − 36 = SMPS C2 (65 Hz). Set `synth_root: C2` so synthesis and output pitch are both authentic:

```yaml
- low:  C5
  high: B6
  mod_instrument: 22
  root: C2
  synth_root: C2    # chip pitch = C2; heard = 65 Hz at C2 period
```

### vibrato — per-entry override

Overrides the channel-level `smpsModSet` vibrato for any note matched by this range entry.
Value is two hex digits mirroring ProTracker effect `4xy` (x = speed nibble, y = depth nibble).

```yaml
voice_map:
  3:
    - low: A6
      high: E7
      mod_instrument: 4
      root: A2
      synth_root: A5
      vibrato: 12      # speed=1, depth=2 (overrides smpsModSet params for this range)
```

| Value | Meaning |
|-------|---------|
| absent | Use channel smpsModSet speed/depth (default) |
| `00` | Suppress vibrato entirely for this entry |
| `XY` | Speed nibble X, depth nibble Y (same encoding as `4xy`) |

YAML accepts integer (`vibrato: 12` → speed=1, depth=2), hex integer (`vibrato: 0x12`), or string (`vibrato: "1A"` → speed=1, depth=10). Also supported on `psg_map` and `psg_voice_map` entries with the same semantics.

### Choosing root

`root` is absolute, so choose it based on where you want the note to land in the MOD pattern — independent of any channel transposition. Ensure the full range `root + (high − low)` stays within C1–B3 (values 0–35).

**Example** — source C5–B6 (`low`=C5, `high`=B6, span=11 semitones):
- `root: C2` (value 12): C5 → C2, B6 → B2. Span 12–23, all in range ✓.
- `root: C3` (value 24): C5 → C3, B6 → B3. Span 24–35, all in range ✓ (higher `target_rate` = better quality).

### When to set `transpose: 0`

If `voice_map` entries cover the full note range of a channel, the base YAML `transpose` is redundant. Set `transpose: 0` and let `root` control pitch placement entirely.

## PSG Instrument Mapping

For full PSG synthesis details (SN76489 internals, normalization, envelope tables, API),
see `docs/psg_synthesis.md`. Note: the older `psg_form_map` key is deprecated; use `psg_map`.

### psg_map

Maps `smpsPSGform` byte values to synthesized PSG instruments. The key is the raw
`smpsPSGform` byte; `type` (white/periodic noise) is **auto-inferred** from bit 2 of the key.
When `smpsPSGform $E7` appears in channel data, the PSG channel switches to the specified instrument.

```yaml
psg_map:
  0xE7:                    # smpsPSGform byte; bit 2=1 → white noise, bits [1:0]=3 → follow tone ch2
    mod_instrument: 7      # MOD instrument slot (1-based)
    root: A2               # MOD anchor — determines target_rate AND where low plays
    low: A3                # SMPS pitch anchor — nA3 → MOD A2; each semitone above/below shifts ±1
    synth_root: A3         # LFSR synthesis freq — sets tone2_n for rate-3; independent of root
    noise_rate: 3          # 0=N/512, 1=N/1024, 2=N/2048, 3=follow tone ch2 (LFSR freq from synth_root)
    envelope: fTone_04     # Named envelope from settings.yaml psg_envelope_tables, or inline list
    base_volume: 0         # SN76489 base attenuation (0=max, 15=silent)
```

SN76489 noise register byte encoding:
- Bits [1:0]: rate — 0=N/512, 1=N/1024, 2=N/2048, 3=follow tone ch2 (LFSR freq set by synth_root)
- Bit [2]: type — 0=periodic noise, 1=white noise (auto-inferred; do not specify manually)
- `$E0`–`$E3` = periodic noise; `$E4`–`$E7` = white noise; `$E7` is most common in Sonic 1

`noise_rate` values:
- `0` = N/512 (fixed LFSR clock, fastest preset)
- `1` = N/1024
- `2` = N/2048
- `3` = follow tone ch2 — synthesizer derives `tone2_n` from `synth_root` (or `root`)

### psg_voice_map

Maps `smpsPSGvoice` envelope labels → MOD instrument numbers. Labels are the symbolic
names from the SMPS assembly (`fTone_01`–`fTone_09` in Sonic 1).

```yaml
psg_voice_map:
  fTone_01: 7     # default square-wave envelope → instrument 7
  fTone_03: 10    # softer attack envelope → instrument 10
```

When `smpsPSGvoice fTone_03` appears in channel data, the PSG channel switches to
instrument 10. Labels not in the map are silently ignored.

## Timing

The relationship between SMPS ticks and MOD rows:

```
MOD row = SMPS tick / ticks_per_row
```

Common SMPS durations with `ticks_per_row: 6`:

| SMPS Duration | Hex | MOD Rows |
|---------------|-----|----------|
| 6 ticks | $06 | 1 row |
| 12 ticks | $0C | 2 rows |
| 18 ticks | $12 | 3 rows |
| 24 ticks | $18 | 4 rows |
| 48 ticks | $30 | 8 rows |

Adjust `ticks_per_row` if the song uses unusual timing divisions.

## BPM Derivation

Set `auto_bpm: true` to derive BPM automatically from the SMPS tempo header instead of guessing.

### How SMPS Tempo Works

The SMPS driver runs on VBlank (60 Hz NTSC, 50 Hz PAL). Each frame:

1. A tempo counter decrements by 1
2. When it hits 0, `TempoWait` fires: resets counter to `modifier`, adds +1 to all tracks' `DurationTimeout`
3. All tracks' `DurationTimeout` is decremented by 1

The net effect: every `modifier` frames, one frame's decrement is cancelled. The effective tick rate is:

```
SMPS_ticks_per_second = fps × (modifier - 1) / modifier
```

Note durations from assembly are also multiplied by the `divider` value at load time.

### Formula

```
BPM = fps × (modifier - 1) × speed × 2.5 / (modifier × divider × ticks_per_row)
```

Where:
- `fps` = 60 (NTSC) or 50 (PAL)
- `modifier` = SMPS header tempo modifier (e.g. 5 for Title Screen)
- `divider` = SMPS header tempo divider (e.g. 1 for Title Screen)
- `speed` = MOD speed / ticks per row (default 6)
- `ticks_per_row` = SMPS ticks per MOD row (default 6)

### Example Values

| Song | Divider | Modifier | NTSC BPM | PAL BPM |
|------|---------|----------|----------|---------|
| Title Screen | 1 | 5 | **120** | 100 |

With `speed = ticks_per_row` (both 6), the formula simplifies to:

```
BPM = fps × (modifier - 1) × 2.5 / (modifier × divider)
```

### Usage

In YAML config:
```yaml
auto_bpm: true
region: ntsc    # or "pal"
```

Or via CLI:
```bash
python convert.py song.asm --auto-bpm --region ntsc
```

## DAC Sample Rates

The Sonic 1 sound driver plays DAC samples via Z80-driven PCM/DPCM routines. Sample rates are embedded in the source WAV files and used by the assembler to compute Z80 DJNZ loop counters (see `s1.sounddriver.asm` and `z80.asm`).

### Native Sample Rates

| Sample | Source File | Native Rate | Format |
|--------|------------|-------------|--------|
| dKick | `dac/dpcm/kick.wav` | 8,250 Hz | 8-bit mono DPCM |
| dSnare | `dac/dpcm/snare.wav` | 24,000 Hz | 8-bit mono DPCM |
| dTimpani | `dac/dpcm/timpani.wav` | 7,375 Hz | 8-bit mono DPCM |

The timpani variants reuse `timpani.wav` at scaled playback rates:

| Variant | Scale | Effective Rate |
|---------|-------|----------------|
| dHiTimpani | ×1.30 | 9,588 Hz |
| dMidTimpani | ×1.20 | 8,850 Hz |
| dLowTimpani | ×0.97 | 7,154 Hz |
| dVLowTimpani | ×0.95 | 7,006 Hz |

### Pitch-Correct MOD Notes

The Amiga Paula chip plays samples at a rate determined by the note's period value. To play a sample at its native pitch, choose the MOD note whose playback frequency (`7,093,789 / (period × 2)` Hz, PAL) best matches the native sample rate.

| Sample | Native Rate | MOD Note | Period | Playback Rate | Error |
|--------|------------|----------|--------|---------------|-------|
| dKick | 8,250 Hz | **C2** | 428 | 8,287 Hz | +0.4% |
| dSnare | 24,000 Hz | **F#3** | 151 | 23,490 Hz | -2.1% |
| dTimpani | 7,375 Hz | **A#1** | 480 | 7,389 Hz | +0.2% |
| dHiTimpani | 9,588 Hz | **D#2** | 360 | 9,853 Hz | +2.8% |
| dMidTimpani | 8,850 Hz | **C#2** | 404 | 8,779 Hz | -0.8% |
| dLowTimpani | 7,154 Hz | **A1** | 508 | 6,982 Hz | -2.4% |
| dVLowTimpani | 7,006 Hz | **A1** | 508 | 6,982 Hz | -0.3% |

Since timpani variants only differ in pitch, they can share a single MOD instrument (e.g. instrument 3) and use different trigger notes. This saves sample slots.

### Preparing Samples for MOD

The source WAV files in `sonic_1/dac/dpcm/` need to be converted to raw 8-bit signed PCM for inclusion in a MOD file. Using SoX:

```bash
sox kick.wav -t raw -e signed -b 8 -c 1 kick.raw
sox snare.wav -t raw -e signed -b 8 -c 1 snare.raw
sox timpani.wav -t raw -e signed -b 8 -c 1 timpani.raw
```

Place the `.raw` files in your `samples_dir` and reference them in `sample_list`. The sample rate argument to SoX is not needed here since MOD playback rate is controlled by the note period, not a header value.

## Example Configs

### Minimal (CLI defaults plus YAML override)

```yaml
name: "Green Hill Zone"
channels:
  - source: FM1
    mod_channel: 0
    transpose: -36
    instrument: 1
```

Unspecified channels are skipped. This converts only FM1.

### Full Sonic 1 Song

See [`configs/title_screen.yaml`](../configs/title_screen.yaml) for a complete example with all 9 channels, per-channel transpose, and DAC sample mappings.
