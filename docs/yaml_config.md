# YAML Configuration Reference

Per-song YAML configs allow fine-grained control over the SMPS-to-MOD conversion. Place them in the `configs/` directory.

## Usage

```bash
python convert.py configs/my_song.yaml
```

CLI `--output` overrides the YAML `output_file` if both are given.

A key the converter does not know is an error, in a song config and in `settings.yaml` alike: a
typo, or a key from an older version, would otherwise be ignored.

## Full Schema

```yaml
# Song name written into MOD header (max 19 characters)
name: "Title Screen"

# File paths
input_file: "path/to/song.asm"   # or a .vgm / .vgz rip: lifted back into the song (core/vgm/lift/;
                                 # docs/todo/vgz_conversion.md), or a ROM (.bin / .md / .gen) with rom_song
# rom_song: "$81"      # ROM input only: the sound ID to read from its bytecode (core/rom/; "$81", "0x81"
                       # or 129).  The data fixes known for that exact ROM are applied, as the asm
                       # applies FixMusicAndSFXDataBugs (core/rom/fixes.py: Sonic 1 rev01 only)
                       # convert.py --input FILE --rom-song ID overrides input_file / rom_song
output_file: "output/title_screen.mod"

# Source driver (VGM input)
driver: sonic1         # the SMPS variant that played the song; sonic1 is the only one (and the default).
                       # An asm input must be sonic1
# tempo_modifier: 3    # VGM input only: override the lift's inferred smpsHeaderTempo modifier (frames
# tempo_divider: 1     # TempoWait holds) / divider, where the log leaves them ambiguous. Each >= 1;
                       # an asm input states its tempo, and setting either there is an error

# Timing
target_bpm: 150        # BPM (32–255), written as Fxx effect on row 0
target_speed: 6        # Ticks per row (1–31), ProTracker default is 6
ticks_per_row: 6       # SMPS ticks per MOD row (controls time scaling)
auto_bpm: true         # Derive BPM from SMPS tempo header (overrides target_bpm)
range_space: source    # what voice_map/psg_voice_map low/high match: "source" (the note byte, default)
                       # or "chip" (the real pitch after pitch_offset + smpsChangeTransposition —
                       # for songs that change key with $E9; see docs/pipeline.md § range_space)
region: ntsc           # Console region: "ntsc" (60 Hz) or "pal" (50 Hz)

# MOD format
# num_mod_channels: 10  # Optional. Derived from the channels section (highest mod_channel + 1,
                       # rounded up to 4/8/10/12/14/16); set it only to pad upward so a spare
                       # channel can carry Fxx (tempo change) / Dxx (loop row) effects
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
# Source WAVs are in reference/smps_drivers/sonic_1/dac/dpcm/; convert to raw signed PCM:
#   sox kick.wav -t raw -r 8250 -e signed -b 8 -c 1 kick.raw
sample_list:
  - [1, "kick.raw", 64, 0]      # [inst_num, filename, volume, finetune]
  - [2, "snare.raw", 64, 0]
  - [3, "timpani.raw", 64, 0]

samples_dir: "./samples/"       # Base path for sample files

# Additional top-level keys (see dedicated sections below for full syntax):
#
# voice_map:             Routes SMPS voice index + note range → MOD instrument + pitch anchor
# channel_instrument_map: Per-channel override for voice_map (e.g. an FM5 double at its own level)
# psg_map:               Maps smpsPSGform bytes → PSG instrument (noise type auto-inferred from key bit 2)
# psg_voice_map:         Maps smpsPSGvoice label → PSG instrument
# mod_pattern_breaks:    Insert Bxx jumps + repack patterns to eliminate blank loop rows
#   - pattern: 0         # Pattern to split
#     row: 31            # Last intro row; body starts at row+1
# merge:                 Channel folding for the Amiga build (convert.py --merged), see below
#   - primary: FM1
#     followers: [FM5]
# merge_patterns:        The same per block of patterns (verse folds ≠ bridge folds), see below
# merge_drop / merge_fill / merge_fill_cut_after: channels left out, or pooled onto silent channels
# merge_output_file:     Where --merged writes (default: output_file stem + "_merged.mod")
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

Routes a SMPS voice index + source-note range to a specific MOD instrument slot, with an optional pitch anchor (`root`). The rendering pitch is derived from the song; `synth_root` may state one (see below).

For synthesis pitch matching (how `root`, `synth_root`, and `low` interact with `target_rate`), see `docs/fm_synthesis.md` §Pitch.

```yaml
voice_map:
  0:                          # SMPS voice index (from smpsSetvoice)
    - low:  G5                # Bottom of source-note range (inclusive)
      high: G6                # Top of source-note range (inclusive)
      mod_instrument: 4       # MOD instrument slot (1-based)
      root: Fs2               # G5 plays at F#2; each semitone above shifts output by 1
      synth_root: C6          # (optional) render at C6 instead of the pitch the chip plays for G5; the sample's rate carries the difference
    - low:  Gs6
      high: C7
      mod_instrument: 12
      root: G3
```

`low`/`high` are SMPS note names (no `n` prefix): `C5`, `Gs6`, `B7`, etc. Range lookup uses `(note_value − $81)`. `smpsAlterNote` is a raw FNUM offset (~10 cents) and does NOT affect range selection or pitch placement.

### root — pitch anchor

`root` is an **absolute** MOD note anchor: the note at which playback sounds at the entry's rendering pitch (`synth_root`). With the rendering pitch derived, that is the pitch the chip plays for `low`, so source `low` plays at `root`, unconditionally — `smpsChangeTransposition` events and header pitch_offset do not affect this. The placement formula is:

```
output = root + (source − low) − synth_shift      # synth_shift is 0 unless synth_root is stated
```

Each semitone above `low` shifts the output up by 1, clamped to C1–B3.

Without `root`, the entry still selects the correct `mod_instrument` but pitch falls through to the channel-transpose path (`smps_note + total_transpose`).

### synth_root — rendering pitch (derived; optional)

Every rooted entry is rendered at the pitch the chip really plays for its `low` note — `low`
plus the pitch offset and every `smpsChangeTransposition`, PSG through the driver's table —
which `core.plan.synth_roots.resolve_synth_roots` reads from the song before synthesis.  No shipped
config states `synth_root`; `convert.py` counts the derived entries and warns
(`synth_root_ambiguous`) where an entry's `low` is played at several chip pitches, which is the
cue for `range_space: chip` or a split entry.

Stating `synth_root` picks a different rendering pitch, anywhere in the range.  The output is
kept in tune by placing every note lower by `synth_shift = synth_root − derived`:

```
target_rate = amiga_clock / PERIOD_TABLE[root]           # unchanged by synth_root
output      = root + (source − low) − synth_shift        # root is where synth_root sounds
```

**Example** — a voice whose chip range is G3–B4, rendered in the middle so the sample is
stretched at most a fifth each way rather than a ninth upwards:

```yaml
- low:  G3
  high: B4
  mod_instrument: 22
  root: C2          # playback at C2 sounds D4
  synth_root: D4    # G3 plays at F1, B4 at A2
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
      vibrato: 12      # speed=1, depth=2 (overrides smpsModSet params for this range)
```

| Value | Meaning |
|-------|---------|
| absent | Use channel smpsModSet speed/depth (default) |
| `00` | Suppress vibrato entirely for this entry |
| `XY` | Speed nibble X, depth nibble Y (same encoding as `4xy`) |

YAML accepts integer (`vibrato: 12` → speed=1, depth=2), hex integer (`vibrato: 0x12`), or string (`vibrato: "1A"` → speed=1, depth=10). Also supported on `psg_map` and `psg_voice_map` entries with the same semantics.

### loop_drift_db / loop_min_ms — per-instrument sustain loops

Where sustain loops are on (`samples.sustain_loops` in settings.yaml; the merged build by default), an
FM sample is cut where its envelope settles plus one loop.  Two entry keys override how, for
that instrument alone:

- `loop_drift_db` — how far above the settled level the loop may freeze (the song's
  `loop_drift_db`, else settings.yaml's).  Lower loops later: past more of the attack, or past
  every note the instrument plays, which then sound as rendered.
- `loop_min_ms` — the shortest loop (30 ms otherwise).  A short loop freezes a detuned voice's
  shimmer into a buzz wherever a note, or its release slide, rings through it.

A merge group takes both too, for its chip composites.  Green Hill lofi:

```yaml
voice_map:
  5:
    - low: C4
      high: B5
      mod_instrument: 11
      root: C2
      loop_drift_db: 1      # at the song's 12 dB it looped 27 ms from 13 ms in
merge_patterns:
  - patterns: "d-10"
    groups:
      - primary: FM3
        followers: [FM4]
        loop_min_ms: 400    # the chords' slow release rang through an 87 ms loop
```

Each costs bytes: `tools/mod_audit.py` shows every sample's loop and size.

### treble_shelf_db — brightness (song level)

`treble_shelf_db: 4` raises every synthesised render 4 dB above settings.yaml's
`treble_shelf_hz` (2500), over the settings' own `treble_shelf_db` (0: off).  An artistic
choice: the renders match the hardware, hand-made Amiga covers are brighter.  The DAC samples
come off disk and are not shelved.

A merge group takes its own `treble_shelf_db` (and `treble_shelf_hz`), on top of the song's,
on its composites only: a mix's whole sum (the drums off disk too), a chip composite's render.
The instruments' own samples elsewhere in the song keep the song's shelf.

A `dac_samples` entry's `saturate_db: 2` (or `merge_saturate_db: 2`, the merged build only,
over `saturate_db` there) soft-clips that drum when it is loaded (tanh, the drive
solved so its RMS rises 2 dB at the same peak) and requantises it to 8 bits: the same peak and
volume, a louder body, some added harmonics.  The drum mixes are built from it.  A compressor
was tried first and dropped: these drums are nearly all peak, so it turned the body down with
the peak (the kick gained 0.4 dB for 3 dB of reduction).  Artistic: the hardware's waveform changes.

A merge group's `limit_db: 4` limits a mix whose sum is past full scale: its peaks (a kick and a
bass attack landing together) come down to full scale by up to 4 dB, with a 1.5 ms lookahead and
a 60 ms release (`core.audio.pcm.limit_peaks`), instead of the whole sound playing that much quieter
at volume 64.  Denser, a little transient distortion.  Green Hill's drum groups: 20 mixes, up to
3.2 dB taken off a peak, none turned down any more.

### Choosing root

`root` is absolute, so choose it based on where you want the note to land in the MOD pattern — independent of any channel transposition. Ensure the full range `root + (high − low)` stays within C1–B3 (values 0–35).

**Example** — source C5–B6 (`low`=C5, `high`=B6, span=11 semitones):
- `root: C2` (value 12): C5 → C2, B6 → B2. Span 12–23, all in range ✓.
- `root: C3` (value 24): C5 → C3, B6 → B3. Span 24–35, all in range ✓ (higher `target_rate` = better quality).

### When to set `transpose: 0`

If `voice_map` entries cover the full note range of a channel, the base YAML `transpose` is redundant. Set `transpose: 0` and let `root` control pitch placement entirely.

### channel_instrument_map — per-channel voice_map override

Overrides `voice_map` for a specific SMPS source channel. Useful when one channel needs
different instrument routing than the global `voice_map` — e.g. FM5 shares FM1's note data
but needs instruments of its own (its smpsAlterNote detune is rendered into whatever samples it
plays, docs/pipeline.md § Detune variants).

Format: `{source_channel: {voice_index: [InstrumentRange, ...]}}`

```yaml
channel_instrument_map:
  FM5:
    4:                        # voice $04 on FM5 only (global voice_map[4] used for all other channels)
      - low:  C6
        high: B7
        mod_instrument: 27    # FM5's own: its detune is rendered into the sample
        root: C2
        synth_root: C5
      - low:  B4
        high: B4
        mod_instrument: 28
        root: B1
        synth_root: B3
```

When a note is played on FM5 with voice $04, `channel_instrument_map["FM5"][4]` is checked
first; `voice_map[4]` is only consulted if no per-channel override exists for that voice.

---

## PSG Instrument Mapping

For full PSG synthesis details (SN76489 internals, normalization, envelope tables, API),
see `docs/psg_synthesis.md`.

### psg_map

Maps `smpsPSGform` byte values to synthesized PSG instruments. The key is the raw
`smpsPSGform` byte, which is the SN76489 noise register: the noise **type** (bit 2) and **rate**
(bits 0–1) are read from it, and the **envelope** is read from the song (the header voice or the
last `smpsPSGvoice`; the label most of the instrument's notes play under).  When `smpsPSGform $E7`
appears in channel data, the PSG channel switches to the specified instrument and stays a noise
channel; a later `smpsPSGvoice` only changes its envelope.

```yaml
psg_map:
  0xE7:                    # smpsPSGform byte: bit 2=1 → white noise, bits [1:0]=3 → follow tone ch2
    mod_instrument: 7      # MOD instrument slot (1-based)
    root: A2               # MOD anchor — determines target_rate AND where low plays
    low: A3                # SMPS pitch anchor — nA3 → MOD A2; each semitone above/below shifts ±1
    envelopes:             # optional: an envelope label that gets its own sample in noise mode
      fTone_08: 18         #   (Scrap Brain's hi-hat); other labels play mod_instrument
    tone2_n: 1             # optional override, rate 3 only: explicit tone-ch2 divider (1–1023)
    synth_root: A3         # optional override, weaker than tone2_n: LFSR rate as a note name
    envelope: fTone_04     # optional override of the derived envelope: a name (fTone_01–fTone_09,
                           #   core/smps/driver_tables.py) or an inline list
    base_volume: 0         # SN76489 base attenuation (0=max, 15=silent); the default
```

A stated `type` or `noise_rate` that contradicts the key byte warns and is ignored.  One instrument
played with several envelopes and no variant for the others warns (`noise_envelopes`).

With rate 3 the converter works the LFSR clock out from the song: PSG3 keeps writing its
own note's divider to tone channel 2, so the value is `PSGFrequencies[note − $81 + transpose]` from
the driver's table.  Sonic 1's `nMaxPSG` (the usual hi-hat note) is entry 69 = 223721 Hz, a divider
of **0**, which the Sega VDP PSG clocks as N=1 (112 kHz shift rate, near-white hiss).  `convert.py`
prints the divider it used; `tone2_n` and `synth_root` exist only as overrides, and no shipped
config uses them.  To see what a recording does:
`python tools/vgm_analyze.py <song>.vgz --chip psg --channel NOISE` prints `white/tone2 N=<n>` for
every noise key-on.

SN76489 noise register byte encoding:
- Bits [1:0]: rate — 0=N/512, 1=N/1024, 2=N/2048, 3=follow tone ch2 (LFSR freq set by synth_root)
- Bit [2]: type — 0=periodic noise, 1=white noise (auto-inferred; do not specify manually)
- `$E0`–`$E3` = periodic noise; `$E4`–`$E7` = white noise; `$E7` is most common in Sonic 1

Rate values (bits 0–1 of the key byte):
- `0` = N/512 (fixed LFSR clock, fastest preset)
- `1` = N/1024
- `2` = N/2048
- `3` = follow tone ch2 — `tone2_n` if given, else `synth_root` if given, else derived from the song through the driver's `PSGFrequencies` table (the normal case; see `docs/psg_synthesis.md`)

### psg_voice_map

Maps `smpsPSGvoice` envelope labels → MOD instrument numbers. Labels are the symbolic
names from the SMPS assembly (`fTone_01`–`fTone_09` in Sonic 1).

```yaml
psg_voice_map:
  fTone_01:
    mod_instrument: 8    # required
    root: A2             # required — MOD anchor; determines target_rate
    synth_root: A4       # optional synthesis pitch override
  fTone_03:
    mod_instrument: 9
    root: A2
    synth_root: A4
```

`root` and `mod_instrument` are required fields. `synth_root`, `low`, `high`, `envelope`
(defaults to the label), `base_volume`, and `vibrato` are optional. A list of entries
(range-split) is also accepted. Entries are always tones: when `smpsPSGvoice fTone_03` appears in
channel data on a tone channel, the PSG channel switches to the specified instrument; on a noise
channel (after `smpsPSGform`) the label only changes the envelope and this map is not consulted —
give the label its own sample with `psg_map[<byte>].envelopes` instead. A noise `type` here is an
error. Labels not in the map are silently ignored.

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

### Rounding, and choosing `target_speed`

The MOD's BPM is a whole number.  When the formula does not land on one the song runs a fraction
of a percent off the hardware: Special Stage at speed 3 is 98.4375 → 98 (−0.44 %, 139 ms behind
over a 33 s pass).  `target_speed` changes how many MOD ticks a row has, not the row grid, so pick
the speed that makes the BPM (nearly) whole — `convert.py` prints the error and the better speed,
and the `analyze.py` skeleton chooses it (`core.config.bpm_rounding_options`).  Special Stage:
speed 6 → 196.875 → 197 (+0.06 %); Star Light / Chaos Emerald: speed 4 → 250 exactly.

Songs with `smpsSetTempoMod` (Drowning, Credits) get an `Fxx` at every change, each segment's BPM
scaled from the header's; every segment must fit 32–255, which is why Drowning uses
`ticks_per_row: 2` (75 → 135 BPM; one tick per row would need 270 at the end).

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

Pre-converted `.raw` files (`kick.raw`, `snare.raw`, `timpani.raw`) are already in `samples/`.
Reference them directly in `sample_list` — no conversion step needed.

MOD playback rate is controlled by the trigger note's period value, not a sample-rate header,
so the `.raw` files contain raw 8-bit signed mono PCM with no header.

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

See [`configs/01_title_screen.yaml`](../configs/01_title_screen.yaml) for a complete example with all 9 channels, per-channel transpose, and DAC sample mappings.

## merge — folding channels for the Amiga build

The reference MOD keeps every SMPS channel. The Amiga port wants 3 or 4 channels with one left
free for sound effects, so a config can name groups of channels that fold onto one:

```yaml
merge:
  - primary: FM1          # the lead ...
    followers: [FM5]      # ... and its detuned double
  - primary: FM4
    followers: [FM3]      # chord stabs: FM3 a third / fourth above FM4 on every note
  - primary: DAC
    followers: [PSG3]     # the hi-hat lands on the drum hits
    cut_primary: true     # a hat over a drum's decay plays and cuts the tail (Green Hill)
  - primary: FM5
    followers: [FM4]
    max_composites: 4     # optional memory budget: the 4 most-played chords; the rest play FM5 alone
    fill_lost: true       # optional: the follower notes this group cannot fold go to the fill pool
    fill_cut: true        # optional: a follower note the fold would cut short plays whole on a channel
                          #   silent for all of it, when there is one (else it folds as before)
merge_output_file: output/01_title_screen_4ch.mod   # optional
merge_drop: [FM3, PSG1]                              # optional: left out of the merged build altogether
merge_fill: [PSG2]                                   # optional: the fill pool — each note on whichever output
                                                     #   channel is silent when it starts; lost where none is
merge_fill_cut_after: {DAC: 2, FM2: 4}               # optional: a pool note may cut these channels' notes after
                                                     #   that many ticks (a kick's decay, a bass note's second half)
merge_max_synth_shift: 0                             # optional: render no sample above its root's pitch (half the bytes)
merge_loop_timbre: true                              # optional: loops wait for the timbre to hold, as the reference build's do
merge_tolerance: 1                                   # ticks a follower note-on may be off the primary's (default 1)
```

`python convert.py configs/01_title_screen.yaml --merged` then writes the reduced MOD: the
followers are dropped, the remaining channels are packed onto MOD channels 0..n-1 in their
configured order (set `num_mod_channels` to pad, e.g. to leave a fourth channel free), and the
primary plays a **composite instrument** wherever a follower sounds with it. Composites take
free instrument slots and are named `merge FM1+FM5` in the sample list. Nothing else in the
config changes, and the plain `convert.py` run is unaffected, so the reference MOD, its
baselines and audits stay the ground truth.

- **Two FM voices** (FM1+FM5, FM4+FM3) are rendered together on the YM2612, one chip channel
  per voice keyed at once, at the follower's interval, `smpsDetune` and level relative to the
  primary's. One composite per distinct interval: the stabs need one per third, fourth and
  fifth. The chip sums and clips them as the hardware does.
- **Anything else** (DAC + PSG hi-hat, FM + PSG tone) is mixed from the finished samples at the
  primary's playback rate, the follower at its `sample_list` volume and baked level. A sum that
  passes full scale plays at volume 64 and is reported.
- The primary's effects apply to the composite: its vibrato, note fill, `Cxx` and `EDx`.
- A follower note that starts while the primary is silent is placed on the merged channel as
  the follower's own note (its instrument, pitch and level), so two channels that never sound
  at once can share a MOD channel outright. A drum or noise note sounds for its sample, not
  its SMPS duration, so a hat two ticks after a kick's sample has ended is such a note.
- `merge_tolerance` (ticks, default 1) lets a follower that starts a tick off the primary
  still fold, and folds a grace note of that length onto the `smpsNoAttack` note it bends into,
  so chords are matched on the pitches they land on. Green Hill's FM3 chord tone starts one
  tick after FM4 and FM5; without this it counted as lost.
- `max_composites: N` on a group keeps only its N most-played composite instruments (the
  converter says which chords are left to the primary alone); the sample bytes are the
  price of every distinct interval, so this is the memory budget for a chord channel.
- `cut_primary: true` on a group lets a follower note that starts while the primary still
  sounds play anyway, cutting the primary's tail; that is what a hi-hat does to a drum's decay
  on a 4-channel Amiga, and it is how Green Hill's 262 hats ride its 172 drum hits. Off, those
  notes are lost (`orphan`).
- Instruments no note of the merged build plays are not rendered, so the merged MOD carries
  only the samples it uses. `merge_max_synth_shift` (merged build only) caps how far above
  its root's pitch a sample is rendered: the reference build renders at the busiest note, up
  to an octave up, which doubles a sample's rate and bytes; `0` renders at the root's pitch
  for half the size, at the cost of envelopes running twice as fast an octave up.
- A mixed composite (a drum with its hi-hat) is mixed at, and triggered from, the note of the
  layer that plays fastest, so the hat keeps its bandwidth instead of being resampled down to
  the kick's rate.
- A song with more independent voices than the Amiga has channels needs `merge_drop:` as
  well: those channels are left out of the merged build (they still vote for their
  instruments' levels, so the measured volumes hold). Which parts to drop is a musical
  decision the survey cannot make; Green Hill Zone keeps drums, bass, lead and one harmony.
- `merge_fill:` is the middle way: the **fill pool**. Each note of a pooled channel is placed
  on whichever output channel is silent when it starts — the one that stays silent longest,
  or one whose next note-on cuts it, never for less than a row — with its own instrument,
  pitch and level; a note with no silent channel is lost, and the converter reports per
  channel how many were placed where. A group's `fill_lost: true` sends the follower notes it
  cannot fold (orphans) to the pool too. `merge_fill_cut_after: {channel:
  ticks}` lets a channel's notes count as silent after that many ticks, so a chime may cut a
  kick's decay or a bass note's second half, as a hand-made 4-channel cover would; a channel
  absent there is never cut. A group's `fill_cut: true` sends the follower notes the fold
  would cut short (a chime ring longer than the bass note it rides) to the pool as well, but
  only for a channel silent for the whole note; Green Hill's PSG1 rings play whole on the
  lead's channel while the lead rests (patterns 2–4) and fold into the bass mixes elsewhere. Green Hill's chime lines start on the bass and drum note-ons
  almost every time, so the pool places 30 of 188; they fold onto the bass as bass+chime mixes
  instead.
- Composites share the 31 instrument slots with the instruments the merged build still plays;
  the converter prints how many slots were free and how many the groups asked for. A PSG
  instrument that only survives as a source of the mixes gives up its slot as well (its sample
  is rendered for the mixer and kept out of the table). Over
  budget, the least-played composites go (those whose primary instrument stays anyway first);
  set each group's `max_composites` so the groups' budgets fit the free slots.
- With `samples.sustain_loops` on in `settings.yaml` (the default for `--merged`), every looped sample
  is cut to its loop and every FM note ends with a release slide; see `docs/pipeline.md`
  § Sustain loops.

## merge_patterns — folds that differ per pattern

When the arrangement wants one fold in the verse and another in the bridge (Green Hill: the
bass rides the drum channel through patterns 1–c and has its own channel in 0 and d–10), the
groups are given per block of patterns instead:

```yaml
merge_patterns:
  - patterns: "0"                 # hex, as Fast Tracker shows them: "0", "1-4", "d-10", "0, 5-c"
    drop: [FM4, FM5, PSG1, PSG2]  # optional: their notes in these patterns are left out
    groups:
      - primary: DAC              # a group as in merge:, holding in these patterns only
        followers: [PSG3]
        cut_primary: true
  - patterns: "1-4"
    groups:
      - primary: DAC
        followers: [FM2, PSG3]
        cut_primary: true
      - primary: PSG1
        followers: [FM3, FM4, FM5]
```

A group may add `mod_channel: 2` (a `channels:` number) or `mod_channel: FM2` (a source): its
primary's notes take that column in the block's patterns, which must be free there: its owner
folded, dropped or itself moved to another column (a group with no followers is a plain move) (Green Hill's chords ride the bass column while the bass rides the
drums), so the finished MOD needs no more columns than the busiest pattern.  A note left ringing
on the borrowed column when the block ends is cut there.

A group with no followers and `fill: true` pools its channel's notes in the block's patterns:
each goes to whichever column in use there is silent when it starts (a group's `cut_after: 8`
lets a pooled note take that group's column once its note is 8 ticks old, cutting the tail;
`merge_fill_cut_after` is the song-wide fallback), and is lost where none is; the channel has no
column of its own in those patterns.  Green Hill's bridge arpeggio, 64 eighth notes, has no
column of its own in a four-column bridge, so it is sprinkled between the drums, lead, bass
and chords instead.

A channel not named in a block keeps its own channel there.  A channel leaves the output only
when it is a follower or dropped in every pattern the blocks name; otherwise it stays, its
column empty in the patterns it folds in (its notes play on the primary's column) and its own
elsewhere.  The output therefore has one channel per source that is live anywhere — Green
Hill's table gives 8 — which is what a hand-finish in a tracker wants: every column keeps its
meaning.  A fold lands on its primary's column.  Patterns the blocks do not name fold nothing
and are reported; a channel in two groups of one pattern is an error; `merge:` groups may sit
beside `merge_patterns:` (they hold in every pattern).  A YAML integer is a decimal pattern
number; write strings for hex.

A group may add `bank: true`: its mixed composites (kick+bass+hat, bass+chime and the like)
share instrument slots as **sample banks**, every note starting with `9xx` at its sound's
offset and cut where the sound ends, so eighteen drum mixes cost three slots instead of
eighteen.  A melodic primary's notes give the attack row's slot to the `9xx` (a `Cxx` there
moves a row later, an `EDx` is dropped); a looped mix goes last in its bank.
`merge_bank_slots` (song level) is how many slots the composite fit holds back for them:
leave it out (`auto`, the default) and the converter works it out, building again once the
banks' sizes are known; a number pins it. A group's `mix_at: primary` makes its mixes at the
primary's own note, so a looped primary keeps its loop and a lead under a chime costs a few KB
instead of its whole note unrolled (the chime is resampled down into it).  A group's
`mix_note: F2` caps the note its mixes are made at: a mix is made at its fastest layer's note
(the hat's A3, 28 kHz) unless that is above the cap, so F2 (11 kHz) makes the drum mixes 2.5
times smaller at the cost of the hat's treble above 5.5 kHz. `merge_twins: always` (song
level; default `short`) gives every chord shape one mix even when slots are to spare: a
composite whose shape (voices and intervals) another has plays that one, the one whose
followers ring furthest, instead of a mix of its own that differs only in where a follower is
cut (by default that happens only while the composites do not all fit). A group's
`loop_mix: true` loops its long mixes where the sum settles, found in the finished mix as a
single voice's sustain loop is, with an 80 ms crossfade (the layers beat, so the join lands on
another phase of the beat); the group's `loop_drift_db` / `loop_min_ms` steer it.  Lossy: the
chord's slow movement freezes in the loop.  Green Hill lofi: its two long chords, 30 KB each,
loop from 1.35 and 1.4 s (drift 1 dB, loops of at least 300 ms). Details:
`docs/pipeline.md` § Sample banks.

`python tools/fold_csv.py configs/02_green_hill_zone.yaml input/02_ghz_fold.csv --write`
(`--bank` adds `bank: true` to every drum-primary group) writes the section from a fold table (`Pattern,Ch 1,...` header; cells `fold N` / `keep` /
`drop` / blank; `fold N*` names that channel the fold's primary), choosing each fold's primary by measurement (drums first, then the fewest
lost follower notes) and printing the counts per candidate; see `docs/pipeline.md` § Per-pattern
folds.

What folds cleanly is a property of the song. `python tools/merge_survey.py configs/<song>.yaml`
lines up every pair of channels and prints, per pair, how many follower notes fold, how many play
on their own (*solo*) and how many are lost (a follower note that starts while the primary
sounds is an *orphan* and needs its own channel; one that keeps ringing under the primary's next
note-on is *held*; one that ends sooner leaves the primary alone), then suggests groups. The converter prints the same counts for the groups it was
given. Rules in `docs/pipeline.md` § Channel merging.
