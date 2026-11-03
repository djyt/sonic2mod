# Song configs

A song config (`configs/<game>/<song>.yaml`) tells `convert.py` what to read and how each of the song's
chip channels becomes MOD channels and instruments.  This page is for anyone writing or tuning one: it starts
with a three-line config, builds up to a full one, then lists every key the loader accepts.  Settings shared
by every song live in `settings.yaml` ([settings.md](settings.md)); sound effects have their own file
([sound_effects.md](sound_effects.md)).

On this page:

1. [Running a config](#running-a-config)
2. [A minimal config](#a-minimal-config)
3. [Building up a full config](#building-up-a-full-config)
4. [Key reference](#key-reference): [song keys](#song-keys), [timing](#timing), [`channels:`](#channels),
   [layout](#layout), [instruments](#instruments-how-a-note-finds-its-sample), [`sample_list`](#sample_list),
   [shaping samples](#shaping-samples), [merged build](#merged-build-keys), [variants](#variants)
5. [Spelling](#spelling)
6. [Pitfalls](#pitfalls)

---

## Running a config

```bash
python convert.py configs/sonic_1/01_title_screen.yaml              # the reference build: every channel
python convert.py configs/sonic_1/01_title_screen.yaml --merged     # the Amiga build (4 channels)
python convert.py configs/sonic_1/02_green_hill_zone.yaml --variant lofi --merged   # a variant
python convert.py configs/streets_of_rage_2                         # every song config in a folder
```

The **reference build** keeps every chip channel on a MOD channel of its own.  The **merged build**
([glossary](../glossary.md#merged-build)) folds channels together to fit an Amiga's 4: see
[amiga_builds.md](amiga_builds.md).

| Flag | Effect |
|---|---|
| `-o`, `--output PATH` | Write here instead of `output_file` (also the merged build's file) |
| `--input FILE`, `--rom-song ID` | Override `input_file` / `rom_song` |
| `--show-config` | Print the config as converted (a minimal one: with what was derived), then convert |
| `--write-config PATH` | Write it to PATH and stop: freezes a derived config to hand-tune |
| `--merged` | Build the merged output |
| `--variant NAME` | Apply the config's `variants: {NAME: ...}` blocks |
| `--settings PATH` | Settings file (default: `settings.yaml` beside the config, else `configs/settings.yaml`) |
| `--player ft2\|pt2` | Override settings.yaml `player` and `merged_player` |
| `-v`, `--verbose` | Also list composites, bank sounds, loop extensions, synthesis pitches, each sample's release rate and share of the song |

Given a folder, `convert.py` converts every YAML in it that names an `input_file` (so `rips.yaml` is skipped),
in parallel, a line each, and exits 1 if any fails.  `--merged` / `--variant` skip the songs without merge
groups / that variant; `--output`, `--input`, `--rom-song`, `--show-config` and `--write-config` are refused.

**Validation.**  The YAML loader refuses a key given twice in one mapping.  A key the converter does not know
is an error everywhere: top level, inside every entry, group and block.  So is a `region`, `range_space`,
`driver`, `mod_note`, `mix_note` or `root` it cannot read.

**Order of application.**

```
YAML read ─► variants: blocks laid over ─► keys parsed and checked
  ─► completed from the song (a minimal config, or row_grid: bar)
  ─► --merged: the merged build's columns ─► auto_bpm: target_bpm from the tempo header
  ─► settings.yaml, with the song's own overrides (loop_drift_db, treble_shelf_db, max_window) ─► --output
```

---

## A minimal config

A config with **no `channels:` key** is minimal ([glossary](../glossary.md#minimal-config)): everything it
leaves out is derived from the song (`core/plan/derive.py`).  Three lines convert a ROM song:

```yaml
name: "Smooth Criminal"
input_file: "input/roms/Michael Jackson's Moonwalker (World) (Rev A).md"
rom_song: "$81"
```

`python convert.py configs/moonwalker/81_smooth_criminal.yaml --show-config` prints the completed config;
`--write-config PATH` freezes it as a full config to hand-tune.

### What is derived

| Key | Derived as |
|---|---|
| `name` | The config file's stem |
| `output_file` | `configs/` mirrored under `output/`: `configs/moonwalker/81_smooth_criminal.yaml` → `output/moonwalker/81_smooth_criminal.mod` (a variant adds `_<NAME>`) |
| `range_space` | `chip` |
| `auto_bpm` | `true` |
| `channels` | Every channel that plays, in header order, MOD channel 0 up |
| `ticks_per_row` | The coarsest row grid that keeps the most note starts on rows |
| `target_speed` | The speed whose whole-number BPM is nearest the driver's tempo, 2–8; 2–16 where none of 2–8 is within 0.1 % (Streets of Rage's 13-frame rows play at speed 13) |
| `voice_map`, `psg_voice_map` | One rooted entry per [window](../glossary.md#window) of the pitches each voice / envelope plays |
| `psg_map` | A slot per noise form, rendered with the envelope most of its notes play; `envelopes:` a slot for each other one |
| `dac_samples` | ROM input only: a slot per DAC sample hit, at the note and finetune nearest its rate (`dac_stall` applied); Type 0 FM drums rendered whole at `samples.drum_root` |
| `samples_dir` | `samples/` beside the output file (DAC samples are written there) |
| `sample_list` | A row per slot, at a starting volume (the level the window's notes mostly play at) |

### Windows

A **window** is a span of a voice's pitches that one sample serves.  Its `root` sets every note's playback
rate, and so how many harmonics fit below Nyquist.

- Its lowest pitch goes on the first MOD note whose rate keeps `samples.root_harmonics` (8) harmonics below
  Nyquist, never under E1 (about 5.2 kHz).  A bass stays at E1; a lead moves up and its sample grows with
  its rate.
- It ends at `samples.top_note` (A3), narrowing (to an octave at least) to reach it.
- Its span is at most `samples.max_window` semitones (shipped 9): a sample played d semitones from its
  render pitch runs its envelope 2^(d/12) times too fast or slow.  The song key `max_window` replaces the setting
  (Golden Axe's `hq` variants: the narrowest each song fits in 31 slots without a merge).

### When the slots run out

A MOD holds 31 instruments.  Where the windows need more, these give way in turn, each a warning
(`core/plan/slot_fit.py`).  Among copies, the one whose notes sound the fewest ticks goes first.

1. An envelope the volume shapes plays at its most played volume, `Cxx` the difference (Streets of Rage 2's
   ADSR envelopes).
2. Voice copies under the hardware LFO play as their plain voice ([detune.md](../conversion/detune.md)).
3. A voice's neighbouring windows merge, the cheapest first: those notes play further from their sample's pitch.
4. Copies with a register patch play as the voice they patch (the patch can change the sound wholly).
5. Copies in channel 3's special mode play as their plain voice: the operator offsets, and the notes' pitch
   with them, are lost.

A song that still does not fit is an error.  The plain build does not use [banks](../glossary.md#bank).

### Keys that steer the derivation

All but `row_grid` matter only where something is derived (a minimal config, or derived DAC notes).

| Key | Default | What it does |
|---|---|---|
| `row_grid` | `accurate` | `accurate`: the finest grid the notes need (the build to check against a rip).  `bar`: a composer's layout from the song's meter, a 16th a row and whole phrases a pattern; it derives `ticks_per_row`, `target_speed` and `mod_pattern_breaks`, so stating them is an error (`null` them in the variant).  Works on any config.  Rules: [timing.md](../conversion/timing.md#row_grid-bar) |
| `window_root` | `lowest` | `lowest`: each window's root on the first note keeping `root_harmonics` harmonics (smallest samples).  `highest`: as high as the window reaches and the sample limit allows, its longest ring and release whole: two to three times the bytes (Streets of Rage 2, Time Trax) |
| `max_window` | settings | Replaces `samples.max_window` for this song; 0 = no cap |
| `level_patch_min` | `0` | TL steps a volume-law voice copy (differing in its TLs alone) must differ from its voice by somewhere to keep a slot; the rest play as their voice at their level.  0 = every copy keeps one |
| `baked_slots` | `0` | Slots kept free (as far as the steps above free them) for samples with a sweep or volume envelope baked in (`core/plan/baked.py`); the most played shapes take them |
| `dac_stall` | `0` | Share (0 to under 1) of the DAC's time the driver's update holds its play loop: derived DAC notes play at the loop rate × (1 − it).  Measure it: `python tools/vgm/vgm_analyze.py RIP --dac-stall` (OutRun: 0.33–0.40) |

### Overriding a derived item

A stated item replaces only the derived item it names: one voice's `voice_map` list, one `psg_map` form, one
`psg_voice_map` label, one `sample_list` row.  `dac_samples`, stated, replaces the derived list whole.

A `sample_list` row naming a derived file (`fm_v04_C3.raw`, `psg_noise_e7.raw`, `dac81.raw`) follows that
file to whatever slot it now has: a setting that splits a window renumbers the slots after it.  A row naming a
derived file the song no longer has is dropped (the report names it); any other file replaces its slot's row.
A window with no row starts at the level its notes mostly play at, scaled by its voice's nearest measured
window's correction.  `vgm_compare --write-volumes` ([verifying.md](verifying.md#per-instrument-levels)) finds rows by file, adds
rows for windows that have none, and writes each derived file's current slot:

```yaml
sample_list:                 # volumes measured against the rip; the rest derived
  - [4, "fm_v00_Bb2.raw", 18, 0]
  - [18, "psg_noise_e7.raw", 3, 0]
```

---

## Building up a full config

A **full config** states its `channels:` and instrument maps.  Start from the skeleton `analyze.py` writes:

```bash
python analyze.py "reference/smps_drivers/sonic_1/music/Mus81 - GHZ.asm" --write configs/sonic_1/my_ghz.yaml
```

Green Hill Zone (`configs/sonic_1/02_green_hill_zone.yaml`) is the worked example below, trimmed.

**1. What to read and where to write.**

```yaml
name: "Green Hill Zone"
input_file: "input/sonic_1/Mus81 - GHZ.asm"
output_file: "output/sonic_1/02_green_hill_zone.mod"
samples_dir: "input/sonic_1/samples/"     # the DAC drums' .raw files
```

**2. Timing.**  A row is 2 SMPS ticks; the speed is 3 MOD ticks a row; the BPM comes from the song's tempo
header.  Rules: [timing.md](../conversion/timing.md).

```yaml
auto_bpm: true
region: ntsc
target_speed: 3
ticks_per_row: 2
```

**3. Channels.**  Each chip channel to convert, and its MOD column (0-based):

```yaml
channels:
  - {source: DAC,  mod_channel: 0}
  - {source: FM1,  mod_channel: 1}
  # ... FM2-FM5 on 2-5
  - {source: PSG1, mod_channel: 6}
  - {source: PSG2, mod_channel: 7}
  - {source: PSG3, mod_channel: 8}       # noise ($E7) in this song
```

**4. Instruments.**  Route each voice's notes to a slot, anchored with `root` (the MOD note the range's `low`
plays at):

```yaml
voice_map:
  5:                       # voice $05: main melody, split in two ranges
    - {low: C4, high: B5, mod_instrument: 9, root: C2}
    - {low: C6, high: B7, mod_instrument: 8, root: C2}
channel_instrument_map:
  FM5:                     # FM5's voice $04 gets its own slot (its detuned double of FM4)
    4:
      - {low: C6, high: B7, mod_instrument: 7, root: C2}
psg_voice_map:
  fTone_01: {mod_instrument: 15, root: A2}
psg_map:
  0xE7: {mod_instrument: 14, root: A3}   # every noise hit plays A3
dac_samples:
  - {name: dKick,  mod_instrument: 1, mod_note: B1}
  - {name: dSnare, mod_instrument: 2, mod_note: F3}
```

**5. Samples.**  A row per slot: file, volume, finetune.  Synthesised slots use only the volume and finetune;
DAC slots load the file.

```yaml
sample_list:
  - [1, "kick.raw",    64, -2]
  - [2, "snare.raw",   55, 1]
  - [9, "ghz_v05_lo.raw", 7, 0]
```

**6. Layout.**  The intro fills rows 0–31 of pattern 0; a `Bxx` there jumps to pattern 1:

```yaml
mod_pattern_breaks:
  - {pattern: 0, row: 31}
```

**7. The merged build** (optional): which channels fold together, per block of patterns.  How to write it:
[amiga_builds.md](amiga_builds.md#per-pattern-folds); keys: [below](#merged-build-keys).

```yaml
merge_patterns:
  - patterns: "1-4"
    groups:
      - primary: DAC
        followers: [FM2, PSG3]
        cut_primary: true
        bank: true
```

**8. Variants** (optional): a smaller build that differs in a few keys ([below](#variants)):

```yaml
variants:
  lofi:
    name: "Green Hill Zone lofi"
    merge_twins: always
    loop_drift_db: 20
```

Then measure the volumes against the rip and verify the notes: [verifying.md](verifying.md).

---

## Key reference

### Song keys

| Key | Default | What it does |
|---|---|---|
| `name` | `"Untitled"` (minimal: file stem) | MOD title (20 characters kept) |
| `input_file` | required | `.asm`; a `.vgm` / `.vgz` rip (lifted back into a song, [glossary](../glossary.md#lift)); or a ROM (`.bin` / `.md` / `.gen`) with `rom_song`.  Relative to the working directory |
| `rom_song` | — | ROM input only: the sound ID (`"$81"`, `"0x81"` or `129`).  Known data fixes for that exact ROM apply (`core/rom/fixes.py`) |
| `driver` | detected (ROM); `sonic1` (asm, rip) | `sonic1`, `smps68k_type1a` (Moonwalker), `smpsz80_type0fm` (Golden Axe), `smps68k_streets_of_rage`, `smpsz80_sh2` (Space Harrier II), `smpsz80_outrun`, `mucom_md` (Streets of Rage 2), `timetrax` |
| `tempo_modifier`, `tempo_divider` | inferred | `.vgm` / `.vgz` input only: override the lift's tempo (each ≥ 1) |
| `output_file` | `<input stem>.mod` in the working directory (minimal: mirrored under `output/`) | Where the MOD is written; spaces in the input stem become `_` |
| `samples_dir` | `./samples/` | Where `sample_list` files are read from (relative to the working directory) |

### Timing

Formulas and the speed choice: [timing.md](../conversion/timing.md).

| Key | Default | What it does | Example |
|---|---|---|---|
| `ticks_per_row` | `6` | SMPS ticks per MOD row, counted as the asm's duration bytes (before the tempo divider): the row grid.  Pick the GCD of the song's durations | GHZ: `2` |
| `auto_bpm` | `false` | Derive the BPM from the tempo header (every shipped config sets it) | `true` |
| `target_bpm` | `150` | BPM when `auto_bpm` is off; written as `Fxx` on row 0 | `125` |
| `target_speed` | `6` | MOD ticks per row.  Leaves the row grid alone; choose it so the BPM is (nearly) whole: `convert.py` prints a better speed | GHZ: `3` |
| `region` | `ntsc` | `ntsc` or `pal`: the V-int rate (60 / 50 Hz) or FM clock, whichever the driver's update runs on | `pal` |

### `channels:`

One entry per chip channel to convert; a channel not listed is skipped.

| Key | Default | What it does |
|---|---|---|
| `source` | required | `DAC`, `FM1`–`FM6`, `PSG1`–`PSG3` (the names the song's header gives) |
| `mod_channel` | required | 0-based MOD column |
| `transpose` | `0` | Semitones added on the [transpose path](#instruments-how-a-note-finds-its-sample) only |
| `instrument` | `1` | Slot for notes no map entry routes |
| `volume` | `64` | Scales every volume the channel writes (× volume / 64) |
| `enabled` | `true` | `false` skips the channel |

### Layout

| Key | Default | What it does |
|---|---|---|
| `mod_pattern_breaks` | none | `[{pattern: 0, row: 31}]`: that row ends the pattern with a `Bxx` to the next and the rest is repacked from there.  [song_structure.md](../conversion/song_structure.md) |
| `num_mod_channels` | derived | Pad the MOD to 4 / 8 / 10 / 12 / 14 / 16 channels (at least the highest `mod_channel` + 1, rounded up to one of those) |
| `max_patterns` | `127` | Writing stops at this pattern |

### Instruments: how a note finds its sample

```
 DAC note ─── dac_samples[name] ─────────────────────────────────────► slot + mod_note

 FM note ──── voice ─┬─ channel_instrument_map[source][voice] ─┐
                     └─ voice_map[voice] ──────────────────────┴─► the entry whose low..high
                                                                   holds the note ─► slot
 PSG tone ─── envelope label ── psg_voice_map[label] ─────────────► entry ─► slot
 PSG noise ── form byte ─────── psg_map[form] (envelopes: by label) ► entry ─► slot
```

Then the MOD note:

| The entry has | MOD note |
|---|---|
| `root` (for PSG: and `low`) | `root + (note − low)`: the **root path** ([glossary](../glossary.md#root-path)) |
| `root`, noise | `root`, always (with `low`: the root path) |
| no `root`, or no entry | SMPS note + header pitch offset + `smpsChangeTransposition` + channel `transpose`: the **transpose path** (index 0 = C1) |

A result outside C1–B3 is clamped with a warning.  Each synthesised slot's sample is rendered for the **first**
entry that names it.  When to anchor with `root`: [notes.md](../conversion/notes.md).

#### `range_space`

What `low` / `high` / `root`'s anchor are compared with ([glossary](../glossary.md#range_space)).

| Value | Compared with |
|---|---|
| `source` (default; minimal configs: `chip`) | the note byte (`nC5` = `C5`) |
| `chip` | the pitch the chip plays: byte + pitch offset + `smpsChangeTransposition` (PSG through the driver's table) |

Use `chip` for a song that changes key with `$E9` while keeping a voice.  `tools/config/config_to_chip_space.py`
converts a source-space config.

#### `voice_map`

```yaml
voice_map:
  0:                      # voice index (smpsSetvoice): 5, $05 or 0x05
    - low: G5             # SMPS note names
      high: A6
      mod_instrument: 3
      root: G2            # the MOD note low plays at
```

| Key | Default | What it does |
|---|---|---|
| `low`, `high` | required | The range, inclusive |
| `mod_instrument` | required | MOD slot 1–31 |
| `root` | none | Root-path anchor; without it the entry only names the slot |
| `synth_root` | derived | The pitch the sample is rendered at ([glossary](../glossary.md#synth_root)): derived as the pitch the entry's notes play most.  Stating it changes the sample's rate, never where notes land.  [fm.md](../synthesis/fm.md) |
| `vibrato` | from `smpsModSet` | `4xy` parameter as two hex digits: `12`, `1A`, `0x12`; `0` = none |
| `loop_drift_db`, `loop_min_ms`, `loop_start_ms`, `loop_decay`, `dither`, `name` | | [Shaping samples](#shaping-samples) |

`smpsAlterNote` / `smpsDetune` never affects the range lookup: detuned notes get a
[detune variant](../glossary.md#detune-variant) of the entry's sample ([detune.md](../conversion/detune.md)).

#### `channel_instrument_map`

The same entries for one channel only, checked before `voice_map`:

```yaml
channel_instrument_map:
  FM5:                    # FM5 shares FM4's notes but plays at its own level
    4:
      - {low: C6, high: B7, mod_instrument: 7, root: C2}
```

#### `psg_voice_map`

PSG tone instruments by envelope label (`smpsPSGvoice`, or the header's voice; `$00` = none).  A value is one
entry or a list split by `low` / `high`.  Not consulted on a channel in noise mode.

| Key | Default | What it does |
|---|---|---|
| `mod_instrument`, `root` | required | Slot and anchor.  Without `low`, `root` only sets the sample's rate and the note takes the transpose path |
| `low`, `high` | none | Range, and the root-path anchor |
| `envelope` | the label | Envelope override: `fTone_01` … `fTone_09`, or an inline list |
| `base_volume` | `0` | SN76489 attenuation the sample is rendered at (0 loudest, 15 silent) |
| `synth_root`, `vibrato`, `dither`, `name` | | As in `voice_map` |
| `type` | `tone` | Any other value is an error, as is `noise_rate` (a noise envelope goes under `psg_map`'s `envelopes`) |

A label not in the map leaves the current instrument playing.  The loop keys (`loop_*`) are not accepted here.

#### `psg_map`

Noise instruments, keyed by the `smpsPSGform` byte (`0xE7`, `$E7`).  The byte is the SN76489 noise register:
bit 2 white / periodic, bits 0–1 the rate (3 = clocked by PSG3's tone, the
[tone-2 divider](../glossary.md#tone-2-divider)).  Once a channel takes
`smpsPSGform` it stays a noise channel; a later `smpsPSGvoice` only changes the envelope.

```yaml
psg_map:
  0xE7:
    mod_instrument: 17
    root: A3                  # the MOD note every hit plays
    envelopes: {fTone_08: 18} # this label gets its own sample (Scrap Brain's hi-hat)
```

| Key | Default | What it does |
|---|---|---|
| `mod_instrument`, `root` | required | Slot; `root` is every hit's MOD note |
| `low`, `high` | none | With `low`, hits follow the note: `root + (note − low)` (Marble Zone's pitched noise) |
| `envelopes` | `{}` | `{label: slot}`: an envelope that gets its own sample |
| `envelope` | derived | Override the envelope read from the song |
| `tone2_n` | derived | Rate 3 only: the tone-channel divider (1–1023) clocking the noise, over the one the song gives.  [psg.md](../synthesis/psg.md) |
| `synth_root` | derived | Rate 3 only: the tone pitch clocking the noise (C3–Gs8, the driver's table; `tone2_n` wins) |
| `base_volume`, `vibrato`, `dither`, `name` | | As above |
| `type`, `noise_rate` | from the byte | Redundant: one that disagrees with the byte warns and is ignored |

#### `dac_samples`

```yaml
dac_samples:
  - name: dKick
    mod_instrument: 1
    mod_note: B1
```

| Key | Default | What it does |
|---|---|---|
| `name` | required | The driver's DAC sample name.  Sonic 1: `dKick`, `dSnare`, `dTimpani`, `dHiTimpani`, `dMidTimpani`, `dLowTimpani`, `dVLowTimpani`; a ROM's: as `--show-config` lists them (`dac81`; Type 0 FM drums `drum81`) |
| `mod_instrument` | required | Slot; the timpani share one and differ by `mod_note` |
| `mod_note` | `C3` | Trigger note: `Fs3`, `F#3`, `Gb3` all work |
| `saturate_db` | `0` | Soft-clip the drum so its RMS rises this many dB at the same peak |
| `merge_saturate_db` | — | The same, merged build only (replaces `saturate_db` there) |

Pick the `mod_note` and `sample_list` finetune whose Amiga rate (`3546895 / period × 2^(finetune/96)`) is
nearest the hardware's.  The rate depends on the song (the 68k stalls the Z80 for a share of each frame), so
each config picks its own.  Sonic 1's configs: kick B1, snare F3, timpani Hi D2 / Mid C2 / Low A1 / VLow Gs1.
The rates: [sonic_1.md](../drivers/sonic_1.md).

### `sample_list`

`[slot, "file.raw", volume, finetune]`: one row per slot; volume (0–64, default 64) and finetune (−8…7,
12.5 cents a step, default 0) may be left off.

- **Synthesised slot** (FM or PSG with synthesis on): only volume and finetune are used; the file name labels it.
  Volumes are measured: `tools/vgm/vgm_compare.py --write-volumes` ([verifying.md](verifying.md#per-instrument-levels)).
- **Disk sample** (DAC drums, or synthesis off): the file, signed 8-bit mono PCM, from `samples_dir`.
- With baked levels (the default) the volume is the level of the instrument's most common `smpsAlterVol` / pan
  state; other notes get `Cxx` ([levels.md](../conversion/levels.md)).
- The last row for a slot wins; a synthesised slot with no row plays at 64.

### Shaping samples

Optional keys that trade bytes for fidelity.  "FM entry" = a `voice_map` or `channel_instrument_map` entry;
"group" = a merge group, applied to its composites.

| Key | Where | Default | What it does |
|---|---|---|---|
| `loop_drift_db` | FM entry, group, song | `samples.loop_drift_db` (1) | How far above the settled level a [sustain loop](../glossary.md#sustain-loop) may freeze (dB).  Lower: loops later, more faithful, more bytes.  The song key replaces the setting |
| `loop_min_ms` | FM entry, group | 30 | Shortest loop.  Raise it where a short loop buzzes (GHZ lofi: 300) |
| `loop_start_ms` | FM entry, group | 0 | Earliest loop start: keeps a beating pair's loop out of its attack |
| `loop_decay` | FM entry, group | `freeze` | `slide`: loop a voice that keeps fading early and write the fall as volume slides (FM only) |
| `dither` | any entry, group | `samples.dither` | `shaped` / `flat` / `off` (a bare YAML `off` works) |
| `name` | any entry, group | generated | MOD sample name (22 characters).  A group's composites get their note added |
| `treble_shelf_db` | song, group | `samples.treble_shelf_db` (0) | Brightness shelf above `treble_shelf_hz`.  The song key replaces the setting; a group's adds to it |
| `treble_shelf_hz` | group | `samples.treble_shelf_hz` (2500) | The group's shelf corner |
| `saturate_db`, `merge_saturate_db` | `dac_samples` entry | 0 | [above](#dac_samples) |
| `limit_db` | group | — | Limit a mix's peaks past full scale by up to this many dB instead of turning the whole mix down |

Sustain loops and release slides: [samples.md](../conversion/samples.md).  `tools/mod/mod_audit.py` shows each
sample's loop and size.

### Merged-build keys

What each key does, in one line.  How to build a merged config, and the rules: [amiga_builds.md](amiga_builds.md).
The terms ([fold](../glossary.md#fold), [primary](../glossary.md#primary),
[follower](../glossary.md#follower), [composite](../glossary.md#composite), [mix](../glossary.md#mix),
[fill pool](../glossary.md#fill-pool), [bank](../glossary.md#bank)) are defined there and in the glossary.

```yaml
merge:                            # song-wide groups
  - primary: FM1                  # the channel that carries the fold
    followers: [FM5]              # folded onto it as composite instruments
merge_patterns:                   # groups per block of patterns
  - patterns: "1-4"               # reference-build patterns, hex as trackers show them
    drop: [PSG2]                  # these channels' notes are left out here
    groups:
      - primary: DAC
        followers: [FM2, PSG3]
      - primary: FM5
        followers: [FM3, FM4]
        mod_channel: FM2          # take FM2's column here
```

#### Cheat sheet

| Key | Where | Default | What it does |
|---|---|---|---|
| `patterns` | block | required | Reference-build patterns in hex: `"0"`, `"1-4"`, `"d-10"`, `"0, 5-c"`, or a list.  A bare YAML number is **decimal** |
| `drop` | block | `[]` | Channels whose notes are left out in the block's patterns |
| `groups` | block | `[]` | The folds and moves that hold in those patterns |
| `primary` | group | required | The channel that carries the fold: its column, fill and vibrato |
| `followers` | group | `[]` | Channels folded onto it, as composites or solo notes.  Empty only with `mod_channel`, `fill` or `bank_drums` |
| `mod_channel` | block group | the primary's | The column (a `channels:` number or a source such as `FM2`) the primary's notes take here; it must be free there (its owner folded, dropped or moved).  No followers: a plain move |
| `cut_primary` | group | `false` | A follower note over the primary's tail plays and cuts it (a hat over a drum's decay) |
| `bank` | group | `false` | The group's mixes (not chip composites) share slots as sample banks, chosen with `9xx`; a melodic note gives its attack row's effect to `9xx` |
| `bank_drums` | group | `false` | A drum primary's lone hits go into the banks too (each drum a sound), freeing its slots for composites.  Implies `bank`; needs no followers |
| `mix_note` | group | the fastest layer's | Cap on the MOD note a mix is made at: bytes vs treble (C2 loses the hat, F2 keeps it to 5.5 kHz) |
| `mix_at` | group | — | `primary`: a mix is made at the primary's own note, so a looped lead / bass / chord keeps its loop |
| `max_composites` | group | none | Keep the N most-played composites; the rest use a same-shape stand-in or the primary alone |
| `fill_lost` | group | `false` | Follower notes the fold cannot place go to the fill pool |
| `fill_cut` | group | `false` | Follower notes the fold would cut short go to the pool, whole notes only |
| `fill` | block group | `false` | No followers, no `mod_channel`: the channel's notes go to whichever column in use is silent when each starts; lost where none is |
| `cut_after` | block group | — | A pooled note may take this group's column once its note is N ticks old |
| `fm_on_chip` | group | `true` | A mix's FM primary and FM followers render together on the chip, the rest mixed on top; `false` sums the looped samples |
| `loop_mix` | group | `false` | A long mix loops where its sum settles (lossy; for a pitched primary) |
| `limit_db`, `treble_shelf_db`, `treble_shelf_hz`, `loop_drift_db`, `loop_min_ms`, `loop_start_ms`, `loop_decay`, `dither`, `name` | group | the primary's entry's, then the song's | [Shaping samples](#shaping-samples), for the group's composites |
| `merge_output_file` | song | `<output stem>_merged.mod` | Where `--merged` writes (not suffixed by a variant) |
| `merge_drop` | song | `[]` | Channels left out of the merged build everywhere |
| `merge_fill` | song | `[]` | The fill pool: each note on whichever column is silent when it starts |
| `merge_vacate` | song | `[]` | Channels that keep their column but move each note to one silent for all of it (nothing cut but a release slide); frees a column for a game's sound effects |
| `merge_fill_cut_after` | song | `{}` | `{channel: ticks}` (at least 1): a pool note may cut that column's notes once this old; song-wide `cut_after` |
| `merge_tolerance` | song | `1` | Ticks a follower note-on may be off the primary's and still fold |
| `merge_bank_slots` | song | `auto` | Slots held back for banks; `auto` rebuilds once with what the banks need, a number pins it |
| `merge_twins` | song | `short` | `short`: same-shape composites share a slot only while slots are short; `always`: always |
| `merge_max_synth_shift` | song | `12` | Cap (semitones) on how far above its root a sample is rendered; `0` halves shifted samples |
| `merge_loop_timbre` | song | `false` | The merged build's loops wait for the timbre to hold, as the reference build's do (bigger samples) |

"Block group" = a `merge_patterns` group only: `mod_channel`, `fill` and `cut_after` are errors in `merge:`.
`merge:` groups may sit beside `merge_patterns:`.  A channel stays in the output unless it is a follower or
dropped in every pattern the blocks name.

Tools (each takes `--variant NAME`; `fold_csv.py --write` does not):

```bash
python tools/config/merge_survey.py <config> --all     # every channel pair: what folds cleanly, suggested groups
python tools/config/fold_csv.py <config> <table.csv> --write --bank --mix-note C3   # the section from a fold table
python convert.py <config> --merged                    # read "composite slots" and the warnings
python tools/mod/mod_audit.py output/<song>_merged.mod --banks   # samples vs the notes that play them
```

### Variants

A build that differs in a few keys (Green Hill's `lofi`) is a [variant](../glossary.md#variant) of the config,
not a copy.  Any mapping may hold a `variants:` block; `--variant NAME` lays its `NAME` entry over the keys
beside it.  Without `--variant` the blocks are ignored.

```yaml
variants:
  lofi:
    name: "Green Hill Zone lofi"
    merge_twins: always

merge_patterns:
  - patterns: "1-4"
    groups:
      - primary: DAC
        followers: [FM2, PSG3]
        mix_note: C3
        variants:
          lofi: {mix_note: A2}    # this group only
```

- Keys **replace**: a list or nested mapping is restated whole; `null` removes a key.  To change one key of a
  nested mapping, put the block inside that mapping.  A variant cannot hold variants.
- `output_file` gains `_<NAME>`: `<stem>_<NAME>.mod` (merged: `<stem>_<NAME>_merged.mod`).  An `output_file` the
  variant block states, and any `merge_output_file`, are used as written.
- An unknown NAME is an error listing the known ones.
- `--variant` is taken by `convert.py`, `analyze.py --config`, `tools/vgm/vgm_compare.py`,
  `tools/vgm/vgm_pitch_audit.py`, `tools/config/merge_survey.py` and `tools/config/fold_csv.py`.
  `vgm_compare.py --write-volumes` and `fold_csv.py --write` refuse it (they edit the base config).
- Regression cases for a variant: a `variant:` line in `tests/cases.yaml`.

---

## Spelling

| Where | Accepts | Examples |
|---|---|---|
| `low`, `high`, `synth_root` | SMPS note names, sharps as `s`, flats as `b`, no `#` (`synth_root` past B7 too) | `Cs6`, `Db6`, `C8` |
| `root`, `samples.top_note`, `samples.drum_root` | MOD notes C1–B3, sharps as `s` only | `Cs2` (not `C#2`, `Db2`) |
| `mod_note`, `mix_note` | MOD notes C1–B3, any spelling | `Fs3`, `F#3`, `Gb3` |
| `voice_map` keys, `psg_map` keys, `rom_song`, `--rom-song` | `$81` or `0x81` (hex), or digits (decimal) | `$05`, `0x05`, `5` |
| `vibrato` | hex digits, read as written | `12`, `1A`, `0x12` |
| `patterns` | hex in a string; a bare number is decimal | `"d-10"`, `"0, 5-c"` |

---

## Pitfalls

- `channels.transpose` acts only on the transpose path: an entry with `root` ignores it.
- `root` ignores key changes under `range_space: source`; a song that changes key with `$E9` wants `chip`
  ([notes.md](../conversion/notes.md)).
- `synth_root` never moves a note, only the sample's rate.
- A `merge_patterns` group written without its leading `- ` would merge into the group above; the loader
  refuses the duplicate key that results.
- A stale MOD fools every audit: convert again before checking.
