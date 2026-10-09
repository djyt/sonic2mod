# YAML configuration reference

Two files drive a conversion:

- a **song config** (`configs/<song>.yaml`): what to read, and how each SMPS channel becomes MOD
  channels and instruments;
- **`settings.yaml`**: synthesis and sample settings shared by every song (§ 8).

This page lists every key: what to write, and in a line or two what it does.  The mechanics
live in `docs/pipeline.md` (conversion), `docs/fm_synthesis.md` / `docs/psg_synthesis.md`
(sample rendering) and `docs/smps_driver.md` (the hardware).

1. [Running a config](#1-running-a-config)
2. [Minimal config](#2-minimal-config)
3. [A full config: one MOD channel per SMPS channel](#3-a-full-config-one-mod-channel-per-smps-channel)
4. [Instruments: how a note finds its sample](#4-instruments-how-a-note-finds-its-sample)
5. [Shaping samples](#5-shaping-samples)
6. [The merged (Amiga) build](#6-the-merged-amiga-build)
7. [Variants](#7-variants)
8. [settings.yaml](#8-settingsyaml)
9. [Spelling notes and pitfalls](#9-spelling-notes-and-pitfalls)

---

## 1. Running a config

```bash
python convert.py configs/01_title_screen.yaml              # the reference build: every channel
python convert.py configs/01_title_screen.yaml --merged     # the Amiga build (§ 6)
python convert.py configs/02_green_hill_zone.yaml --variant lofi --merged   # a variant (§ 7)
```

| Flag | Effect |
|---|---|
| `-o`, `--output PATH` | Overrides `output_file` (also the merged build's file) |
| `--input FILE`, `--rom-song ID` | Override `input_file` / `rom_song` |
| `--show-config` / `--write-config PATH` | Print / write the completed config (§ 2) and stop |
| `--merged` | Build the merged output (§ 6) |
| `--variant NAME` | Apply a `variants:` block (§ 7) |
| `--settings PATH` | Use this settings file (default: `settings.yaml` beside the config, else `configs/settings.yaml`) |
| `--player ft2\|pt2` | Overrides `player` in settings.yaml |
| `-v`, `--verbose` | Also list composites, bank sounds, loop extensions, synthesis pitches |

**Validation.**  The YAML loader refuses a key given twice in one mapping.  A key the converter
does not know is an error everywhere — top level, inside every entry and group, and in each
settings.yaml section — as is a `region`, `range_space` or `mod_note` it cannot read.

**Order of application.**  `variants:` overlay → song keys parsed → (no `channels:`) the
minimal config completed from the song → `--merged` → `--output` → `auto_bpm` → settings, then
the song's own overrides of them.

---

## 2. Minimal config

A config with **no `channels:` key** is minimal: everything it leaves out is derived from the
song (`core/plan/derive.py`).

```yaml
name: "Smooth Criminal"
input_file: "input/roms/Michael Jackson's Moonwalker (World) (Rev A).md"
rom_song: "$81"
sample_list:                 # optional: volumes measured against the VGZ
  - [4, "fm_v00_Bb2.raw", 24, 0]
```

Derived: `name` (file stem), `output_file` (`output/` mirroring `configs/`),
`range_space: chip`, `auto_bpm: true`, `channels` (every channel that plays, in header order),
`ticks_per_row` and `target_speed` (the speed whose whole BPM is nearest the driver's tempo, 2-8,
up to 16 where none of those is within 0.1 %: Streets of Rage's 13-frame rows play at speed 13),
`voice_map` / `psg_voice_map` (one rooted entry per window of the pitches each voice plays),
`psg_map` (a slot per noise form, rendered with the envelope most of its notes play, and
`envelopes:` a slot for each other one), `dac_samples` (ROM input only) and `sample_list`
(starting volumes).

A window's `root` sets every one of its notes' rates, and so how many harmonics fit below
Nyquist (the same count for each note of the window).  Its lowest pitch goes on the first MOD note
whose rate keeps `samples.root_harmonics` harmonics of it (never under E1, the audit's 5 kHz
line); the window ends at `samples.top_note` (A3) and narrows, to an octave at least, to reach
that note; one that still cannot sits as high as it fits.  `samples.max_window` caps every
window's span: a sample played d semitones from the pitch it was rendered at runs its envelope
2^(d/12) times too fast or slow (`docs/todo/user_improvements.md` item 7).  A bass voice stays at E1; a lead moves
up and its sample grows with its rate (§ 8).

A stated item replaces only the derived item it names: one voice's `voice_map` list, one
`psg_map` form, one `sample_list` row.  A row naming a derived sample's file (`fm_v04_C3.raw`,
`psg_noise_e7.raw`, `dac81.raw`) replaces that sample's row wherever its slot is now: a setting
that splits a window renumbers the slots after it, and the row follows its file.  One naming a
derived file the song no longer has (another setting's window, or one the song lost) is left out,
and the report names it; any other file replaces the row of its slot.  A derived window with no row starts at the level its own notes
mostly play at, scaled as its voice's nearest measured window was (measured / starting volume):
a window `max_window` splits off keeps its measured neighbour's correction.  `vgm_compare
--write-volumes` finds a row by its file too, adds rows for windows that have none, and writes
every derived file's current slot.  `--show-config` prints the derived slots; `--write-config`
freezes the result as a full config.

---

## 3. A full config: one MOD channel per SMPS channel

The reference build keeps every SMPS channel on a MOD channel of its own.
`configs/01_title_screen.yaml` is a complete example.

### Song keys

| Key | Default | Meaning |
|---|---|---|
| `name` | `"Untitled"` | MOD title (19 characters kept) |
| `input_file` | — | `.asm`; a `.vgm` / `.vgz` rip (lifted back into a song, `docs/todo/vgz_conversion.md`); or a ROM (`.bin` / `.md` / `.gen`) with `rom_song`.  Relative to the working directory |
| `rom_song` | — | ROM input only: the sound ID, `"$81"`, `"0x81"` or `129`.  Data fixes known for that exact ROM are applied (`core/rom/fixes.py`) |
| `driver` | `sonic1` | The SMPS variant: `sonic1`, `smps68k_type1a` (Moonwalker).  A ROM's is detected |
| `tempo_modifier`, `tempo_divider` | inferred | VGM input only: override the lift's tempo (each ≥ 1) |
| `output_file` | `<input name>.mod` | Where the MOD is written |
| `samples_dir` | `./samples/` | Where `sample_list` files are read from (relative to the working directory) |

### Timing

| Key | Default | Meaning |
|---|---|---|
| `ticks_per_row` | `6` | SMPS ticks per MOD row, counted as the asm's duration bytes (before the header's tempo divider): the row grid.  Pick the GCD of the song's durations |
| `auto_bpm` | `false` | Derive the BPM from the SMPS tempo header (every shipped config sets it) |
| `target_bpm` | `150` | BPM when `auto_bpm` is off; written as `Fxx` on row 0, not range-checked |
| `target_speed` | `6` | MOD ticks per row.  Changes nothing in the row grid; choose it so the derived BPM is (nearly) whole — `convert.py` prints the better speed |
| `region` | `ntsc` | `ntsc` (60 Hz) or `pal` (50 Hz) |

Formulas, tempo changes and the speed choice: `docs/pipeline.md` § Timing.

### `channels:`

One entry per SMPS channel to convert; a channel not listed is skipped.

```yaml
channels:
  - source: DAC
    mod_channel: 0
  - source: FM1
    mod_channel: 1
```

| Key | Default | Meaning |
|---|---|---|
| `source` | required | `DAC`, `FM1`–`FM5`, `PSG1`–`PSG3` |
| `mod_channel` | required | 0-based MOD column |
| `transpose` | `0` | Semitones added on the transpose path only (a note no entry anchors with `root`, § 4) |
| `instrument` | `1` | Fallback slot for notes no map entry routes |
| `volume` | `64` | Scales every volume the channel writes (× volume / 64) |
| `enabled` | `true` | `false` skips the channel |

### Layout

| Key | Default | Meaning |
|---|---|---|
| `mod_pattern_breaks` | none | `[{pattern: 0, row: 31}]`: the intro ends at that row, a `Bxx` jumps to the next pattern and the body is repacked from there.  `docs/pipeline.md` § Pattern breaks (`mod_pattern_breaks`) |
| `num_mod_channels` | derived | Pad the MOD to 4 / 8 / 10 / 12 / 14 / 16 channels (at least the highest `mod_channel` + 1) |
| `max_patterns` | `127` | Writing stops at this pattern |

---

## 4. Instruments: how a note finds its sample

```
 DAC note ────────────── dac_samples[name] ───────────────────────────→ slot + mod_note

 FM note ── voice ─┬─ channel_instrument_map[source][voice] ─┐
                   └─ voice_map[voice] ──────────────────────┴─ entry whose low..high
                                                               holds the note → slot

 PSG tone ─ label ──── psg_voice_map[label] ──────────────────→ entry → slot
 PSG noise ─ form ──── psg_map[form byte] (envelopes: by label) → entry → slot
```

Then the MOD note:

| The entry has | MOD note |
|---|---|
| `root` (and, for PSG, `low`) | `root + (note − low)` — the **root path** |
| `root`, noise | `root`, always |
| no `root`, or no entry | SMPS note + header pitch offset + `smpsChangeTransposition` + channel `transpose` — the **transpose path** (index 0 = C1) |

A result outside C1–B3 is clamped with a warning.  When to anchor with `root`, and why `root`
ignores key changes under `range_space: source`: `docs/pipeline.md` § `voice_map` routing.

Each synthesised slot's sample is rendered for the **first** entry that names it.

### `range_space`

| Value | `low` / `high` / `root` are matched on |
|---|---|
| `source` (default) | the note byte (`nC5` = `C5`) |
| `chip` | the pitch the chip plays: byte + pitch offset + `smpsChangeTransposition` (PSG through the driver table) |

Use `chip` for a song that changes key with `$E9` while keeping a voice;
`tools/config_to_chip_space.py` converts a source-space config.  `docs/pipeline.md` § `range_space: chip`.

### `voice_map`

```yaml
voice_map:
  0:                      # voice index (smpsSetvoice): 5, $05 or 0x05
    - low: G5             # SMPS note names: G5, Cs6, Eb4 (no #)
      high: A6
      mod_instrument: 3
      root: G2            # MOD note C1..B3 where low plays: G2, Cs2 (no #, no flats)
```

| Key | Default | Meaning |
|---|---|---|
| `low`, `high` | required | The range, inclusive |
| `mod_instrument` | required | MOD slot 1–31 |
| `root` | none | Root path anchor (above); without it the entry only names the slot |
| `synth_root` | derived | Rendering pitch (`docs/fm_synthesis.md` § Pitch: synth_root, synth_shift, target_rate).  Derived from the song (the pitch its notes play most, at most an octave above `low`); stating it changes the sample's rate, never where notes land |
| `vibrato` | from `smpsModSet` | Two hex digits `XY` = `4xy` (`12`, `1A` or `0x12`); `0` = none |
| sample keys | | `loop_drift_db`, `loop_min_ms`, `loop_start_ms`, `loop_decay`, `dither`, `name` (§ 5) |

`smpsAlterNote` / `smpsDetune` never affects range lookup: detuned notes get a variant of the
entry's sample automatically (`docs/pipeline.md` § Detune variants).

### `channel_instrument_map`

The same entries, for one channel only; checked before `voice_map`.

```yaml
channel_instrument_map:
  FM5:                    # FM5 shares FM1's data but plays at its own level
    0:
      - {low: G5, high: A6, mod_instrument: 4, root: G2}
```

### `psg_voice_map`

PSG tone instruments, by envelope label (`smpsPSGvoice` or the `smpsHeaderPSG` voice).  A value
is one entry or a list split by `low` / `high`.  Ignored on a channel in noise mode.

| Key | Default | Meaning |
|---|---|---|
| `mod_instrument`, `root` | required | Slot and anchor.  Without `low`, `root` only sets the sample's rate and the note takes the transpose path |
| `low`, `high` | none | Range, and the root-path anchor |
| `envelope` | the label | Envelope override: `fTone_01`…`fTone_09` or an inline list |
| `synth_root`, `base_volume` (0), `vibrato`, `dither`, `name` | | As in `voice_map`; `base_volume` is the SN76489 attenuation rendered at |

A label not in the map leaves the current instrument playing.  `type` other than `tone`, or any
`noise_rate`, is an error.

### `psg_map`

Noise instruments, keyed by the `smpsPSGform` byte (`0xE7`).  The byte is the SN76489 noise
register: bit 2 white / periodic, bits 0–1 the rate.  Once a channel takes `smpsPSGform` it stays
a noise channel; a later `smpsPSGvoice` only changes the envelope.

```yaml
psg_map:
  0xE7:
    mod_instrument: 7
    root: A3              # the MOD note every hit plays
    envelopes: {fTone_08: 18}   # a label that gets its own sample (Scrap Brain's hi-hat)
```

| Key | Default | Meaning |
|---|---|---|
| `mod_instrument`, `root` | required | Slot; `root` is every hit's MOD note |
| `low`, `high` | none | With `low`, hits follow the note: `root + (note − low)` (Marble Zone's pitched noise) |
| `envelopes` | `{}` | `{label: slot}` variants |
| `envelope` | derived | Override of the envelope read from the song |
| `tone2_n`, `synth_root` | derived | Rate 3 only: override the tone-2 divider the song gives (`docs/psg_synthesis.md` § Rate-3 noise: the tone-2 divider) |
| `base_volume`, `vibrato`, `dither`, `name` | | As above |

`type` / `noise_rate` are redundant with the byte: one that disagrees warns and is ignored.

### `dac_samples`

```yaml
dac_samples:
  - name: dKick
    mod_instrument: 1
    mod_note: B1
```

| Key | Default | Meaning |
|---|---|---|
| `name` | required | SMPS DAC name: `dKick`, `dSnare`, `dTimpani`, `dHiTimpani`, `dMidTimpani`, `dLowTimpani`, `dVLowTimpani` |
| `mod_instrument` | required | Slot; timpani variants share one and differ by `mod_note` |
| `mod_note` | `C3` | Trigger note (`Fs3`, `F#3`, `Gb3` all work) |
| `saturate_db`, `merge_saturate_db` | `0` | § 5 |

Pick the `mod_note` and `sample_list` finetune whose Amiga rate (`3546895 / period × 2^(ft/96)`)
is nearest the hardware's: kick B1, snare F3, timpani A1 / D2 / C2 / G#1 in the shipped configs.
The rate depends on the song (the 68k stalls the Z80 for a share of each frame), so each config
picks its own: the smallest finetune within 4 cents of the best for the worst sample on the slot.
The rates and why the VGZ rips read 2–3 % fast: `docs/smps_driver.md` § DAC playback rates.

### `sample_list`

`[slot, "file.raw", volume, finetune]` — one row per slot.

- **Synthesised slot** (FM or PSG with synthesis on): only `volume` (0–64) and `finetune`
  (−8…7, 12.5 c a step, optional) are used.  Volumes are set by measurement:
  `tools/vgm_compare.py --write-volumes` (`docs/pipeline.md` § Verifying against a VGZ).
- **Disk sample** (DAC drums, or synthesis off): the file, signed 8-bit mono PCM, from
  `samples_dir`.
- With baked levels (the default) the volume is the level of the instrument's most common
  `smpsAlterVol` / pan state; other notes get `Cxx` (`docs/pipeline.md` § Levels).
- The last row for a slot wins; a synthesised slot with no row plays at 64.

---

## 5. Shaping samples

Optional keys that trade bytes or fidelity per instrument.  "Entry" = a `voice_map`,
`channel_instrument_map`, `psg_map` or `psg_voice_map` entry; "group" = a merge group (§ 6),
applied to its composites.

| Key | Where | Meaning |
|---|---|---|
| `loop_drift_db` | entry, group, song | How far above the settled level a sustain loop may freeze (dB).  Lower = loops later, more faithful; the song key replaces `samples.loop_drift_db` |
| `loop_min_ms` | entry, group | Shortest loop (default 30 ms).  Raise where a short loop buzzes |
| `loop_start_ms` | entry, group | Earliest loop start: keeps a beating pair's loop out of its attack |
| `loop_decay` | entry, group | `freeze` (default) or `slide`: loop a voice that keeps fading early and write the fall as volume slides (FM only) |
| `dither` | entry, group | `shaped` / `flat` / `off` (YAML's bare `off` works) over `samples.dither` |
| `name` | entry, group | MOD sample name (22 characters) instead of the generated one |
| `treble_shelf_db` | song, group | Brightness shelf above `samples.treble_shelf_hz`.  The song key replaces the setting; a group's adds to it |
| `treble_shelf_hz` | group | The group's shelf frequency |
| `saturate_db` | `dac_samples` entry | Soft-clip the drum so its RMS rises this many dB at the same peak |
| `merge_saturate_db` | `dac_samples` entry | The same, merged build only (replaces `saturate_db` there) |
| `limit_db` | group | Limit a mix's peaks above full scale by up to this many dB instead of turning the whole mix down |

Sustain loops and slides: `docs/pipeline.md` § Sample length, sustain loops and release slides.  `tools/mod_audit.py` shows each
sample's loop and size.

---

## 6. The merged (Amiga) build

`convert.py --merged` folds channels so the song fits 3–4 Amiga channels.  The reference build
is unaffected.  Rules: `docs/pipeline.md` § The merged build.  Terse key list:
`docs/cheat_sheets/merge_patterns.txt`.

### `merge:` — song-wide groups

```yaml
merge:
  - primary: FM1          # the channel that carries the fold
    followers: [FM5]      # folded onto it as composite instruments
  - primary: DAC
    followers: [PSG3]
    cut_primary: true     # a hat over a drum's decay plays and cuts it
```

### `merge_patterns:` — groups per block of patterns

```yaml
merge_patterns:
  - patterns: "1-4"               # reference-build patterns, hex as trackers show them
    drop: [PSG2]                  # these channels' notes are left out here
    groups:
      - primary: DAC
        followers: [FM2, PSG3]
      - primary: FM5
        followers: [FM3, FM4]
        mod_channel: FM2          # take FM2's column here (its owner rides the drums)
```

`patterns:` takes `"0"`, `"1-4"`, `"d-10"`, `"0, 5-c"` or a list; a bare YAML number is
**decimal**.  Every group starts with `- `.  A channel stays in the output unless it is a
follower or dropped in every pattern the blocks name.  `merge:` groups may sit beside
`merge_patterns:`.  `tools/fold_csv.py` writes the section from a fold table.

### Group keys

| Key | Default | Meaning |
|---|---|---|
| `primary` | required | The channel whose column, fill and vibrato the fold uses |
| `followers` | `[]` | Channels folded onto it (empty only with `mod_channel` or `fill`) |
| `cut_primary` | `false` | A follower note over the primary's tail plays and cuts it |
| `max_composites` | none | Keep the N most-played composites; the rest use a same-shape stand-in or the primary alone |
| `fill_lost` | `false` | Follower notes the fold cannot place go to the fill pool |
| `fill_cut` | `false` | Follower notes the fold would cut short go to the pool, whole notes only |
| `bank` | `false` | The group's mixes share slots as sample banks, chosen with `9xx` |
| `mix_note` | fastest layer's | Cap on the note a mix is made at (bytes vs treble) |
| `mix_at` | — | `primary`: mix at the primary's own note so its loop survives |
| `fm_on_chip` | `true` | FM primary + FM followers in a mix are rendered together on the chip |
| `loop_mix` | `false` | Loop long mixes where the sum settles |
| `mod_channel` | primary's | `merge_patterns` only: the column (a `channels:` number or a source) the primary's notes take; it must be free there |
| `fill` | `false` | `merge_patterns` only, no followers: the channel's notes go to whichever column is silent |
| `cut_after` | — | `merge_patterns` only: a pooled note may take this group's column once its note is N ticks old |
| sample keys | | `loop_*`, `dither`, `name`, `treble_shelf_db` / `_hz`, `limit_db` (§ 5) |

### Song-level merge keys

| Key | Default | Meaning |
|---|---|---|
| `merge_output_file` | `<output stem>_merged.mod` | Where `--merged` writes |
| `merge_drop` | `[]` | Channels left out of the merged build |
| `merge_fill` | `[]` | The fill pool: each note on whichever column is silent when it starts |
| `merge_fill_cut_after` | `{}` | `{channel: ticks}`: a pool note may cut that column's notes once they are this old |
| `merge_tolerance` | `1` | Ticks a follower note-on may be off the primary's and still fold |
| `merge_bank_slots` | `auto` | Slots held back for sample banks; `auto` rebuilds with what the banks need, a number pins it |
| `merge_twins` | `short` | `always`: composites of the same shape share one sample even when slots are free |
| `merge_max_synth_shift` | `12` | Cap (semitones) on how far above its root a sample is rendered; `0` halves bytes |
| `merge_loop_timbre` | `false` | Loops wait until the voice's timbre holds, as in the reference build |

What folds cleanly is a property of the song: `python tools/merge_survey.py <config>` counts it
per channel pair and prints suggested groups.

---

## 7. Variants

A build that differs in a few keys (Green Hill's `lofi`) is a variant of the config, not a copy.
Any mapping may hold a `variants:` block; `--variant NAME` lays its `NAME` entry over the keys
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

- Keys **replace**: a list or nested mapping is restated whole; `null` removes a key.  To change
  one key of a nested mapping, put the block inside that mapping.
- `output_file` defaults to `<stem>_<NAME>.mod` (merged: `<stem>_<NAME>_merged.mod`).  A stated
  `merge_output_file` is not suffixed.
- An unknown NAME is an error listing the known ones.
- `--variant` is taken by `convert.py`, `analyze.py --config`, `tools/vgm_compare.py`,
  `vgm_pitch_audit.py`, `merge_survey.py` and `fold_csv.py`.  `vgm_compare.py --write-volumes`
  and `fold_csv.py --write` refuse it (they edit the base config).
- Regression cases for variants: `_VARIANTS` in `tests/regression.py`.

---

## 8. settings.yaml

Found as `--settings PATH`, else `settings.yaml` beside the config, else
`configs/settings.yaml`.  A value it states is used by every CLI and tool.  With no file the
code defaults apply — synthesis **off**.  `tests/settings.yaml` must state the same keys (the
regression runner exits 2 otherwise); add a new key to both.

### Top level

| Key | Code default | Shipped | Meaning |
|---|---|---|---|
| `amiga_clock` | `3546895` | same | Paula clock: a note's rate is this / its period |
| `fm_volume_scaling` | `baked` | `baked` | `baked`: the instrument's commonest level in its volume, `Cxx` elsewhere.  `absolute` (or `true`), `off` (or `false`): legacy |
| `fm_pan_law_db` | `3.0` | `3` | dB a hard-panned FM note counts quieter |
| `psg_volume_scaling` | `baked` | `baked` | `baked` or `absolute` |
| `legato` | `retrigger` | `retrigger` | `smpsNoAttack` notes: `retrigger` (a note-on), `strict` (`3FF` where the sample allows), `loose` (`3FF` always).  `docs/pipeline.md` § Legato (`smpsNoAttack` before a note) |
| `player` | `ft2` | `ft2` | Which player's vibrato depth `4xy` is fitted to: `ft2` or `pt2` |

### `fm_synthesis:`

| Key | Code default | Shipped | Meaning |
|---|---|---|---|
| `enabled` | `false` | `true` | Render FM instruments on the YM2612 (else load `sample_list` files) |
| `mode` | `ym2612` | `ym2612` | Chip variant: `ym2612` or `ym3438` |
| `clock_rate` | `7670454` | same | YM2612 clock (NTSC) |
| `sustain_duration` | `1.5` | `auto` | Seconds a note is held before key-off; `auto` = each instrument's longest ring, 10 s cap (`docs/fm_synthesis.md` § Length: `sustain_duration: auto`) |
| `release_padding` | `0.5` | `0.5` | Seconds rendered after key-off |
| `detune_variants` | `true` | `true` | Render each `smpsAlterNote` detune as its own sample |
| `threads` | `normal` | `normal` | Render threads: `normal` (cores − 1), `max`, or a number |

### `psg_synthesis:`

| Key | Code default | Shipped | Meaning |
|---|---|---|---|
| `enabled` | `false` | `true` | Render PSG instruments on the SN76489 |
| `clock_rate` | `3579545` | same | SN76489 clock |
| `sustain_duration` | `1.0` | `auto` | As for FM |
| `release_padding` | `0.2` | `0.2` | As for FM |
| `oversample` | `8` | `8` | Chip renders this many times the target rate, then resamples down |

### `samples:`

| Key | Code default | Shipped | Meaning |
|---|---|---|---|
| `max_sample_kb` | `128` | `64` | Largest sample: 128 = the format's 131070 bytes, 64 = original ProTracker's 65534 |
| `sustain_loops` | `merged` | `all` | Which builds loop settled samples and end FM notes with release slides: `off`, `merged`, `all` |
| `loop_drift_db` | `1.0` | `1` | § 5 |
| `dither` | `shaped` | `shaped` | 8-bit quantisation: `shaped`, `flat`, `off` |
| `dc_block` | `false` | `true` | 5 Hz high-pass on every render (the hardware's output is AC-coupled) |
| `pt_zero_bytes` | `true` | `true` | Zero a one-shot sample's first word (ProTracker replays it after the sample ends) |
| `compact_slots` | `merged` | `merged` | Renumber the used slots from 1: `off`, `merged`, `all` |
| `names` | `source` | `source` | Sample names: `source` (what plays it, e.g. `F1/3/4/5 $05 C4-B5`) or `file` (the `sample_list` file name) |
| `treble_shelf_db` / `treble_shelf_hz` | `0` / `2500` | same | Brightness shelf on every render; 0 = off |
| `resample_taps` | `32` | `32` | Resampler kernel width |
| `render_cache` | off | `output/cache` | Directory (relative to the project root) caching chip renders by a hash of their inputs |
| `root_harmonics` | `8` | `8` | Minimal configs: harmonics a window's lowest note keeps below Nyquist (§ 2); 0 = every window at E1, the smallest samples |
| `max_window` | `0` | `9` | Minimal configs: the widest window in semitones, so no note plays far from its render pitch (§ 2); 0 = no cap |
| `top_note` | `A3` | `A3` | Minimal configs: the highest MOD note a window reaches.  A#3 (period 120) and B3 (113) are past Paula's period-124 DMA limit and sound bad on an Amiga |
| `drum_root` | `C3` | `C3` | Minimal configs: the MOD note an FM drum (Type 0 FM's drum programs, rendered whole) is rendered for and played at: its sample's rate (C2 ~8.3 kHz, C3 ~16.6, A3 ~27.9) |

---

## 9. Spelling notes and pitfalls

| Where | Accepts |
|---|---|
| `low`, `high`, `synth_root` | SMPS note names `C0`–`B7`: `Cs6`, `Db6`; no `#` |
| `root` | MOD notes `C1`–`B3`: `Cs2`; no `#`, no flats |
| `mod_note`, `mix_note` | MOD notes: `Fs3`, `F#3`, `Gb3` |
| `voice_map` keys, `psg_map` keys, `rom_song`, `--rom-song` | `$81` or `0x81` (hex), or decimal digits (`129`) |
| `vibrato` | hex digits: `12`, `1A`, `0x12` |

- `channels.transpose` only acts on the transpose path; an entry with `root` ignores it.
- A `merge_patterns` group written without its leading `- ` would merge into the group above;
  the loader refuses the duplicate key that results.
