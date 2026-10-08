# sonic2mod — SMPS-to-MOD Converter

## Git Commits
Never include "Co-Authored-By" trailers in commit messages.

Converts Sonic 1 SMPS music (assembly, ROM bytecode or a VGM rip) to Amiga ProTracker MOD, rendering
its instruments on emulated YM2612 / SN76489 chips.

## Documentation Index

Each topic has one home; the others link to it.

| Document | Contents |
|----------|----------|
| `docs/architecture.md` | **Code structure** — data flow, layering, every package and module, `convert()` step order, MOD binary layout |
| `docs/pipeline.md` | **Conversion rules** — timing / BPM, note range and `voice_map` routing, `range_space`, effect mapping and priority, `EDx`, legato, levels, loop / end, detune variants, sustain loops and release slides, pattern breaks, the merged build (groups, fill pool, composites, per-pattern folds, banks, slots), **verifying against a VGZ** |
| `docs/yaml_config.md` | **Every config key** — song config (minimal configs, channels, the instrument maps, sample shaping, merge keys, variants) and `settings.yaml` (code default vs shipped value) |
| `docs/smps_driver.md` | **Sonic 1 driver and hardware** — coord flags, detune vs transposition, TempoWait, modulation / note fill in frames, voice layout and operator order, DAC sample rates, PSG |
| `docs/smps_format.md` | Assembly syntax and how the parser reads it |
| `docs/fm_synthesis.md` | **YM2612 rendering** — catalogue, synth_root / synth_shift / target_rate, `sustain_duration: auto`, render level and clipping, quantisation, OPN2, ym2612/ API |
| `docs/psg_synthesis.md` | **SN76489 rendering** — tone divider, envelopes, rate-3 noise divider, oversampling, sn76489/ API |
| `docs/sfx_rendering.md` | SFX → WAV offline driver (`sonic2wav.py`), 8-bit Amiga export |
| `docs/mod_effects.txt` | ProTracker MOD effect reference |
| `docs/cheat_sheets/merge_patterns.txt` | Terse list of every merge key |
| `docs/todo/` | Plans: `vgz_conversion.md` (VGM lift), `binary_import.md` (ROM input), `user_improvements.md` |
| `docs/audits/` | Per-song accuracy audits vs VGZ (2026-09): `00_soundtrack_survey.md` overview, `01`–`09` per song |
| `reference/smps_drivers/` | SMPS driver sources (gitignored): `sonic_1/` (driver asm, music, SFX, DAC samples), `sonic_2/` |
| `reference/Nuked-OPN2/` | Cycle-accurate YM2612/YM3438 C emulator |
| `reference/mml2mod-master/` | Reference MML-to-MOD converter (origin of the MOD writer) |

## Project Structure

Full module map: `docs/architecture.md`.

```
sonic2mod/
  convert.py  analyze.py  sonic2wav.py   CLIs: conversion, song analysis, SFX → WAV
  core/        library; layers, each importing only those below it (diagnostics.py: any):
               ui → convert / audit → merge → plan → config → source → vgm / rom → smps / mod
               → chips / audio / render_cache / files.  Imports nothing from sfx/ or the chip packages
    smps/        the IR (SmpsSong), parser, the shared song walk (code.py), driver tables, playback
    rom/ vgm/    ROM bytecode reader; VGM register logs and the lift back to a song
    source/      read_song(path): picks asm / ROM / VGM
    config/      ConversionConfig, settings.yaml, variants
    plan/        DriverState walk (resolve_note), instrument catalogue, synth roots, detune, timeline,
                 minimal-config derivation
    merge/       the merged build: pairing, fill pool, composites, mixes, banks
    convert/     SmpsToModConverter and its planners / writers
    mod/         MOD writer and reader, notes, volume, timing, sample audit
    chips/ audio/  chip facts and level laws; gain, PCM, resampler, sustain loops
    audit/ ui/   VGZ audits' library; reports and CLI chrome
  ym2612/ sn76489/   chip wrappers (C emulators via ctypes) + sample generators
  sfx/         offline SFX driver
  configs/     per-song YAML (+ settings.yaml); configs/moonwalker/ minimal configs
  tools/       analysis and audit utilities (vgm_*, mod_*, merge_survey, fold_csv, rom_import, ...)
  tests/       regression suites + unit tests
  docs/  output/  samples/  input/ (ROMs, not in git)  reference/ (gitignored)
```

## Setup

```bash
pip install pyyaml rich   # external dependencies
pip install ruff pyright vulture  # lint / type checking / dead code (optional; or pip install -e .[dev])
```

Synthesis compiles `ym3438.c` / `sn76489.c` with gcc or MSVC on first use.

## Linting

```bash
ruff check .   # style + lint
pyright        # type checking
python -m vulture   # code nothing uses (settings in pyproject.toml; false positives go in vulture_whitelist.py)
```

## Quick Usage

```bash
# Convert using YAML config (primary usage)
python convert.py configs/01_title_screen.yaml

# Override output path
python convert.py configs/01_title_screen.yaml --output output/title_screen.mod

# Also list every composite, bank sound, loop extension and synthesis pitch (and each sample's
# release rate and share of the song)
python convert.py configs/02_green_hill_zone.yaml --merged --verbose

# The Amiga build: fold the config's merge: groups (7 channels → 4 for the Title Screen) into
# composite instruments and write merge_output_file (default <output>_merged.mod)
python convert.py configs/01_title_screen.yaml --merged
# A variant of a config: its `variants: {lofi: ...}` blocks laid over the keys beside them (Green Hill's
# smaller Amiga build; output output/02_green_hill_zone_lofi_merged.mod).  Every config tool takes --variant
python convert.py configs/02_green_hill_zone.yaml --variant lofi --merged
# Which channel pairs of a song can fold (paired / solo / orphans / held / shorter per pair) + the YAML
python tools/merge_survey.py configs/01_title_screen.yaml            # --all: every pair
# Folds that differ per pattern: a table (rows = reference MOD patterns in hex, columns = Ch 1..N, cells
# fold N / fold N* (this one leads) / keep / drop / blank) → merge_patterns: in the config; --write puts it there between markers
python tools/fold_csv.py configs/02_green_hill_zone.yaml input/02_ghz_fold.csv --write
# Audit the merged build's samples: length vs longest note, loops, unused, banks (no config needed)
python tools/mod_audit.py output/02_green_hill_zone_merged.mod
# Audit the merged build: each MOD channel against the sum of its chip channels (balance, onsets);
# a merge_patterns: config gets a column x pattern-block table (block level, primary's key-ons)
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz" --merged

# A minimal config (name, input_file, rom_song; no channels:) - everything else derived from the song
python convert.py configs/moonwalker/81_smooth_criminal.yaml --show-config
python tools/vgm_compare.py configs/moonwalker/81_smooth_criminal.yaml "reference/vgz/moonwalker/03 - Smooth Criminal.vgz" --write-volumes
# Convert straight from the ROM's bytecode (input/roms/, not in git): config rom_song:, or override
python convert.py configs/02_green_hill_zone.yaml --input input/roms/sonic_rev01.bin --rom-song '$81'
# The ROM's songs and SFX: list them, compare each with its asm, write SMPS2ASM text, extract the DAC samples
python tools/rom_import.py input/roms/sonic_rev01.bin --compare reference/smps_drivers/sonic_1
python tools/rom_import.py input/roms/sonic_rev01.bin --asm output/rom_asm --dac output/rom_dac

# Render all 49 sound effects to 16-bit stereo WAV (no config needed)
python sonic2wav.py --all
python sonic2wav.py --rom input/roms/sonic_rev01.bin   # the same 49 read from the ROM
python sonic2wav.py --all --dry-run          # parse + render + report, write nothing
python sonic2wav.py "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"
python sfx/validate.py                       # tables, resampler, 8-bit chain, ticks, panning

# Export signed 8-bit mono .raw + manifest.yaml for Amiga/Paula → output/sfx8/
python sonic2wav.py --all --8bit
python sonic2wav.py --all --8bit --max-rate 16574   # A500 target, ~half the size
python sonic2wav.py --all --8bit --flat-rate 8287   # one rate for every sample

# Analyse a song (no config needed)
python analyze.py "reference/smps_drivers/sonic_1/music/Mus8A - Title Screen.asm"

# Analyse with config coverage diff
python analyze.py "reference/smps_drivers/sonic_1/music/Mus8A - Title Screen.asm" --config configs/01_title_screen.yaml

# Verify: open output .mod in Fast Tracker 2 Clone (https://16-bits.org/ft2.php)
# Smoke-test synthesis pipeline (writes output/validate_test.raw — load in Audacity):
python ym2612/validate.py
python sn76489/validate.py      # C3 tone + white noise → output/psg_{tone,noise}_test.raw

# Analyse FM channels from a VGM/VGZ game recording (verify synth_root values)
python tools/vgm_analyze.py "reference/vgz/01 - Title Theme.vgz" --chip fm --channel FM1 FM2
# Analyse SN76489 PSG noise channel (compare against title_screen.yaml output)
python tools/vgm_analyze.py "reference/vgz/01 - Title Theme.vgz" --chip psg --channel NOISE
# Show all chips / all channels (rate-3 noise rows show the tone-2 divider, DAC rows show PCM seeks)
python tools/vgm_analyze.py "reference/vgz/01 - Title Theme.vgz" --chip all --max-rows 0
# The log frame by frame (core.vgm.frame_log): keys / fnum / carrier TLs, PSG attenuations, DAC seeks per V-int
python tools/vgm_analyze.py "reference/vgz/02 - Green Hill Zone.vgz" --frames --chip all --channel FM1 PSG1

# Is every note right?  Symbolic, no rendering, self-aligning, exit 1 on a wrong/missing note.  Run this FIRST.
# "inst 8: synth_root is 1 octave too high (243 of 243 notes)" = fix that synth_root; "mixed" = a note problem.
# vgm_compare.py prints the same verdict ("Pitch verdict"); its per-note vgm_c / mod_c columns are audio
# cross-checks that still disagree on grace notes (todo item 3).
python tools/vgm_pitch_audit.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz" --list

# The lift (docs/todo/vgz_conversion.md Phase 1) against the asm: differences per channel and aspect
# (onset, length, note, pitch, voice, level, pan, modulation, fill, noise, dac), repeated ones grouped
python tools/vgm_lift.py "reference/vgz/02 - Green Hill Zone.vgz"
python tools/vgm_lift.py --all --aspects onset           # every rip, a line each (~4 s warm)
python tools/vgm_lift.py --all --aspects onset length note --channels FM   # the FM note bytes and durations

# Audit a conversion against its VGZ: per-note pitch/level, pitch verdict, channel balance, onset timing,
# vibrato rate/depth on long FM and PSG notes, noise spectrum, DAC rate.  Needs VGMPlay 0.51.x unzipped into
# reference/vgz/vgmplay/ (untracked, like the VGZ rips; or --vgmplay DIR / VGMPLAY_DIR) and an
# ffmpeg build with libopenmpt — setup in docs/pipeline.md § Verifying against a VGZ.
# Renders go to output/compare/<config>/; --skip-render reuses them.  Reference renders run in parallel and are
# kept in samples.render_cache (output/cache/vgmplay/), keyed on the VGZ + VGMPlay.ini: a song is rendered once
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz"
# Its "Per-instrument level error" table is what sample_list volumes are set from; --write-volumes applies
# the suggestions to the config (then re-convert and re-run to verify)
python tools/vgm_compare.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz" --write-volumes
# CI-style: JSON results + exit 1 when a threshold is exceeded (also --fail-unmatched N)
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz" --json output/compare/title.json --fail-balance-db 2 --fail-pitch-cents 25

# Every song at once (cores-1 in parallel): convert, one --write-volumes pass, re-convert, verify; prints the
# volumes changed, what is still >= 1 dB off (ceiling / channels-disagree / 2-note ones marked) and the pitch
# verdict per song.  Run after any change to how samples are rendered.  One write pass only: the errors are
# relative to the song's median note, so a further pass drifts the whole song.  Reference renders are reused.
python tools/measure_volumes.py
python tools/measure_volumes.py --only green_hill special_stage --no-write
# Other games: pairs from a map (config stem: rip), configs with hex prefixes
python tools/measure_volumes.py --configs configs/moonwalker --vgz-dir reference/vgz/moonwalker --rips configs/moonwalker/rips.yaml
```


## Regression Testing

Baselines live in `tests/baselines/`.  Every song config is a case; every config with a `merge:`
or `merge_patterns:` section is a second case, `<name>_merged`; variants (`_VARIANTS`) are cases
too, and with the ROM in `input/roms/` four songs are converted from its bytecode (`<name>_rom`).
A converter change is safe only once every case still produces a byte-identical MOD — cells **and** the sample table and data (length, volume, finetune, loop, MD5).
Conversions run as parallel subprocesses: ~14 s with an empty render cache, ~4 s warm.

Every case converts with **`tests/settings.yaml`** (`convert.py --settings`), never
`configs/settings.yaml`, so tuning a song by ear does not move the baselines.  It states every key
the live file has (the runner exits 2 otherwise; add a new setting to both).
`tests/baselines/manifest.yaml` records each baseline's commit, date and the hashes of its settings
and config.

```bash
python tests/regression.py --generate-baselines        # BEFORE a change, while the code is known-good
python tests/regression.py                             # AFTER: diff every case
python tests/regression.py --generate-baselines --only title_screen   # accept one song's change
python tests/regression.py --jobs 4                    # limit parallelism (-j 1: one at a time)
python -m pytest tests -q                              # unit tests (merge rules, detune, vgm, rom, ...)
```

**Workflow for any converter change:**
1. Run `--generate-baselines` while code is known-good.
2. Make the change.
3. Run without flags — PASS means no regressions on channels, and no note the player cannot
   sound that the baseline sounds (`tools/mod_lint.py`: a `3xx` with no sample playing or a
   played-out one, a note on an empty instrument slot; run it on any MOD by hand too).
4. A FAIL is not an acceptance: read each changed cell before regenerating that song's baseline —
   a `3FF` where the sounding sample cannot reach the pitch, or a note on a slot the merged build
   stopped rendering, diffs like any intended change.

A change to one config runs only that song's cases (`--only`).

**The VGM tools have their own suite, `tests/tool_regression.py`**: `vgm_analyze` on all 19 VGZs and
`vgm_pitch_audit` on every baseline MOD, byte for byte, in 5 s; `--with-renders` adds `vgm_compare`.
Run it after any change to `core/vgm/`, `core/mod/timing.py` or a VGM tool.

```bash
python tests/tool_regression.py                          # PASS / FAIL + diff
python tests/tool_regression.py --with-renders           # vgm_compare too
python tests/tool_regression.py --generate-baselines --only analyze_02_frames   # accept one change
```

**Adding a new test case:** append a row to `_SONGS` in `tests/regression.py`:
```python
("20_my_song", "my_song", "my_song", "My Song — what makes it worth testing"),
#  config stem   test name  baseline stem  description
```
To ignore a channel while deliberately changing it, add `"my_song": [8]` to `_CASE_OVERRIDES`
(0-based MOD indices); it is normally empty.  `tools/mod_compare.py` diffs any two MODs
(`compare_mods(a, b, ignore_channels=[8])`).

## Rules that are easy to get wrong

One line each; the linked section has the cause and the detail.

**Reading the song** (`docs/smps_format.md`, `docs/smps_driver.md`)
- Notes are bytes $81–$DF (C0–A#7).  FM labels are real pitches: `nA4` at pitch offset 0 = 440 Hz
  (`f = fnum × (clock/144) × 2^block / 2^21`, A4 = fnum 1083, block 4).  A PSG `nC0` is C3.
- A standalone duration byte **re-keys the last note** at its frequency; after `smpsNoAttack` it is a
  tie; after a rest it rests (the driver cleared the frequency).
- `smpsNoAttack` only skips the next key-off: after a rest or an expired fill the note attacks,
  and every read clears the flag.
- A coordination flag completes a pending note; it applies from the next note.
- Channels fall through labels (FM5 into FM1); only `smpsStop` / `smpsJump` / `smpsFade` /
  `smpsStopSpecial` end one.  A label between a note byte and its duration byte does not separate
  them.
- A channel's loop starts where its own walk reached the jump target; loop extension replays the
  events after the label, not a flag written just before it.
- Operator order: SMPS stores OP4..OP1, `SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)`
  (`core/smps/driver_tables.py`).  Wrong order = "overdriven guitar".

**Converting** (`docs/pipeline.md`)
- One state machine decides what a note plays: `DriverState` + `resolve_note` via `walk_channel`
  (`core/plan/driver_state.py`).  Every pass walks this way; never re-implement it.
- `smpsDetune` / `smpsAlterNote` ($E1) is a raw FNUM offset, **not semitones**: no range lookup;
  rendered as detune variants.  Never stand in for it with `finetune`.
- `root` is unconditional (`root + (key − low)`); don't use it on a channel that changes key with
  `$E9` under `range_space: source` — use the transpose path or `range_space: chip`.
- `synth_root` / `synth_shift` change the sample's rate, never where a note lands.
- `smpsNoteFill` and `smpsModSet` count V-int **frames**, not ticks: `Timeline.ticks_per_frame_at`.
  Ticks are unevenly spaced (`TempoWait`); `EDx` delays are measured in frames.
- A MOD BPM is a whole number: choose `target_speed` so the exact BPM is (nearly) integer.
- Levels are baked: an instrument's commonest level is its `sample_list` volume, `Cxx` elsewhere;
  FM samples are rendered at that TL offset (the chip clips as the hardware does).
- One effect per cell; priority: `Cxx` > `4xy` > note cut > a loop's fall slide.
- Layout order inside `convert()` is fixed: passes → pattern breaks → loop `Bxx` → trim → narrow →
  compact → zero idle words.  Row-0 tempo `Fxx` is placed after every channel.
- Noise mode is permanent once `smpsPSGform` runs; `psg_voice_map` is then never consulted.

**Configs and settings** (`docs/yaml_config.md`)
- settings.yaml values are used everywhere; code constants are only the fallback.
- A config with no `channels:` is minimal: the rest is derived from the song.
- A build that differs in a few keys is a `variants:` block, not a copied config.
- The YAML loader refuses a key given twice (a `merge_patterns` group missing its `- `).
- A key the converter does not know is an error, inside entries and groups too.
