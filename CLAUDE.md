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
| `docs/smps_variants.md` | **Other SMPS drivers** — Moonwalker (68k Type 1a), Golden Axe (Z80 Type 0 FM), Streets of Rage (68k, MUCOM-style track code): what each differs from Sonic 1 by, how it was found, how to add a variant |
| `docs/fm_synthesis.md` | **YM2612 rendering** — catalogue, synth_root / synth_shift / target_rate, `sustain_duration: auto`, render level and clipping, quantisation, OPN2, the device and core/synth API |
| `docs/psg_synthesis.md` | **SN76489 rendering** — tone divider, envelopes, rate-3 noise divider, oversampling, the device and core/synth API |
| `docs/sfx_rendering.md` | SFX → WAV offline driver (`sonic2wav.py`), 8-bit Amiga export |
| `docs/mod_effects.txt` | ProTracker MOD effect reference |
| `docs/cheat_sheets/merge_patterns.txt` | Terse list of every merge key |
| `docs/todo/` | Plans: `vgz_conversion.md` (VGM lift); closed, in `done/`: `binary_import.md` (ROM input), `streets_of_rage.md` (SoR), `scaling.md` (many drivers: review, test selection), `user_improvements.md` |
| `docs/audits/` | Per-song accuracy audits vs VGZ (2026-09): `00_soundtrack_survey.md` overview, `01`–`09` per song |
| `reference/smps_drivers/` | SMPS driver sources (gitignored): `sonic_1/` (driver asm, music, SFX, DAC samples), `sonic_2/` |
| `3rdparty/` | Vendored C emulators the chip devices compile (in git): `nuked-opn2/` (cycle-accurate YM2612/YM3438), `sn76489/` (VGMPlay's PSG) |
| `reference/mml2mod-master/` | Reference MML-to-MOD converter (origin of the MOD writer) |

## Project Structure

Full module map: `docs/architecture.md`.

```
sonic2mod/
  convert.py  analyze.py  sonic2wav.py   CLIs: conversion, song analysis, SFX → WAV
  core/        library; layers, each importing only those below it (diagnostics.py: any):
               ui → convert / audit → merge / synth → plan → config → source → vgm / drivers → rom → smps / mod
               → chips / audio / render_cache → files.  Imports nothing from sfx/; drivers only via source
               (import-linter: pyproject.toml)
    smps/        the IR (SmpsSong, PlaybackRules), parser, the shared song walk (code.py), playback; no driver's tables
    drivers/     the sound drivers, a folder each by family (smps68k/sonic1 ...): registry, detect, read a ROM's songs;
                 reference.py: Sonic 1's tables and SONIC1_RULES (an asm song's and a rip's)
    rom/ vgm/    the ROM framework (readers driven by a driver's description); VGM register logs and the lift
    source/      read_song(path): picks asm / ROM / VGM
    config/      ConversionConfig, settings.yaml, variants
    plan/        DriverState walk (resolve_note), instrument catalogue, synth roots, detune, timeline,
                 minimal-config derivation
    merge/       the merged build: pairing, fill pool, composites, mixes, banks
    synth/       the converter's sample rendering: voices / envelopes through the chip devices → PCM
                 (fm_voice, fm_render, fm_samples, psg_render, psg_samples)
    convert/     SmpsToModConverter and its planners / writers
    mod/         MOD writer and reader, notes, volume, timing, sample audit
    chips/       the chips as MAME keeps devices: facts and level laws (fm.py, psg.py); the emulators
                 ym2612/ (OPN2) and sn76489/ (SN76489), C cores through ctypes; cbuild.py compiles them
    audio/       gain, PCM, resampler, sustain loops
    audit/ ui/   VGZ audits' library; reports and CLI chrome
  sfx/         offline SFX driver
  configs/     settings.yaml; per-song YAML a folder per game: sonic_1/, moonwalker/ (minimal configs), ...
  tools/       analysis and audit utilities (vgm_*, mod_*, merge_survey, fold_csv, rom_import, ...)
  tests/       regression suites; unit tests mirror the code (tests/core/vgm/test_reader.py tests core/vgm/reader.py)
  3rdparty/    vendored C emulators (nuked-opn2/, sn76489/);  build/: their compiled libraries (gitignored);
               prebuilt/: the Windows DLLs, used when no compiler is on PATH (tracked)
  docs/  reference/ (gitignored)
  input/       sonic_1/ (Sonic 1's asm songs, fold CSVs, samples/: its DAC drums); roms/ (every game's ROMs, not in git)
  output/      a folder per game as configs/ (sonic_1/: MODs, sfx/, sfx8/); cache/, compare/ shared
```

## Setup

```bash
pip install pyyaml rich   # external dependencies
pip install ruff pyright vulture import-linter coverage  # lint / types / dead code / layers / test selection (or pip install -e .[dev])
```

Synthesis compiles `3rdparty/`'s `ym3438.c` / `sn76489.c` into `build/` with gcc or MSVC on first use; with
no compiler on PATH it loads `prebuilt/`'s DLLs.  After changing a C source (or `ym3438_batch.c`), copy the
rebuilt `build/*.dll` into `prebuilt/` and commit them with it.

## Linting

```bash
ruff check .   # style + lint
pyright        # type checking
python -m vulture   # code nothing uses (settings in pyproject.toml; false positives go in vulture_whitelist.py)
python -m pytest tests/test_layers.py -q   # import layers (pyproject.toml [tool.importlinter]; or lint-imports)
```

## Quick Usage

```bash
# Convert using YAML config (primary usage)
python convert.py configs/sonic_1/01_title_screen.yaml

# Override output path
python convert.py configs/sonic_1/01_title_screen.yaml --output output/sonic_1/title_screen.mod

# Also list every composite, bank sound, loop extension and synthesis pitch (and each sample's
# release rate and share of the song)
python convert.py configs/sonic_1/02_green_hill_zone.yaml --merged --verbose

# The Amiga build: fold the config's merge: groups (7 channels → 4 for the Title Screen) into
# composite instruments and write merge_output_file (default <output>_merged.mod)
python convert.py configs/sonic_1/01_title_screen.yaml --merged
# A variant of a config: its `variants: {lofi: ...}` blocks laid over the keys beside them (Green Hill's
# smaller Amiga build; output output/sonic_1/02_green_hill_zone_lofi_merged.mod).  Every config tool takes --variant
python convert.py configs/sonic_1/02_green_hill_zone.yaml --variant lofi --merged
# Which channel pairs of a song can fold (paired / solo / orphans / held / shorter per pair) + the YAML
python tools/merge_survey.py configs/sonic_1/01_title_screen.yaml            # --all: every pair
# Folds that differ per pattern: a table (rows = reference MOD patterns in hex, columns = Ch 1..N, cells
# fold N / fold N* (this one leads) / keep / drop / blank) → merge_patterns: in the config; --write puts it there between markers
python tools/fold_csv.py configs/sonic_1/02_green_hill_zone.yaml input/sonic_1/02_ghz_fold.csv --write
# Audit the merged build's samples: length vs longest note, loops, unused, banks (no config needed)
python tools/mod_audit.py output/sonic_1/02_green_hill_zone_merged.mod
# Audit the merged build: each MOD channel against the sum of its chip channels (balance, onsets);
# a merge_patterns: config gets a column x pattern-block table (block level, primary's key-ons)
python tools/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz" --merged

# A minimal config (name, input_file, rom_song; no channels:) - everything else derived from the song
python convert.py configs/moonwalker/81_smooth_criminal.yaml --show-config
python tools/vgm_compare.py configs/moonwalker/81_smooth_criminal.yaml "reference/vgz/moonwalker/03 - Smooth Criminal.vgz" --write-volumes
# Golden Axe (SMPS Z80 Type 0 FM; docs/smps_variants.md): minimal configs, FM drums rendered from the ROM
python convert.py configs/golden_axe/81_wilderness.yaml
# Convert straight from the ROM's bytecode (input/roms/, not in git): config rom_song:, or override
python convert.py configs/sonic_1/02_green_hill_zone.yaml --input input/roms/sonic_rev01.bin --rom-song '$81'
# The ROM's songs and SFX: list them, compare each with its asm, write SMPS2ASM text, extract the DAC samples
python tools/rom_import.py input/roms/sonic_rev01.bin --compare reference/smps_drivers/sonic_1
python tools/rom_import.py input/roms/sonic_rev01.bin --asm output/rom_asm --dac output/rom_dac
# Every song of a ROM (or an asm folder) as read, walked and played, as text: what a reader change moved
python tools/song_dump.py "input/roms/Golden Axe (World) (Rev A).md" --only 81

# Render all 49 sound effects to 16-bit stereo WAV (no config needed)
python sonic2wav.py --all
python sonic2wav.py --rom input/roms/sonic_rev01.bin   # the same 49 read from the ROM
python sonic2wav.py --all --dry-run          # parse + render + report, write nothing
python sonic2wav.py "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"
python sfx/validate.py                       # tables, resampler, 8-bit chain, ticks, panning

# Export signed 8-bit mono .raw + manifest.yaml for Amiga/Paula → output/sonic_1/sfx8/
python sonic2wav.py --all --8bit
python sonic2wav.py --all --8bit --max-rate 16574   # A500 target, ~half the size
python sonic2wav.py --all --8bit --flat-rate 8287   # one rate for every sample

# Analyse a song (no config needed)
python analyze.py "reference/smps_drivers/sonic_1/music/Mus8A - Title Screen.asm"

# Analyse with config coverage diff
python analyze.py "reference/smps_drivers/sonic_1/music/Mus8A - Title Screen.asm" --config configs/sonic_1/01_title_screen.yaml

# Verify: open output .mod in Fast Tracker 2 Clone (https://16-bits.org/ft2.php)
# Smoke-test each chip device, then core/synth through it (16-bit .raw files in output/ — load in Audacity):
python tools/validate_ym2612.py       # A4 tone → validate_test.raw; voice, renderer_test.raw, sample_gen_test.raw
python tools/validate_sn76489.py      # C3 tone + white noise → psg_{tone,noise}_test.raw; psg_render_*, psg_sample_gen_*

# Analyse FM channels from a VGM/VGZ game recording (verify synth_root values)
python tools/vgm_analyze.py "reference/vgz/sonic_1/01 - Title Theme.vgz" --chip fm --channel FM1 FM2
# Analyse SN76489 PSG noise channel (compare against title_screen.yaml output)
python tools/vgm_analyze.py "reference/vgz/sonic_1/01 - Title Theme.vgz" --chip psg --channel NOISE
# Show all chips / all channels (rate-3 noise rows show the tone-2 divider, DAC rows show PCM seeks)
python tools/vgm_analyze.py "reference/vgz/sonic_1/01 - Title Theme.vgz" --chip all --max-rows 0
# The log frame by frame (core.vgm.frame_log): keys / fnum / carrier TLs, PSG attenuations, DAC sample starts (a seek, or bytes after a pause) per V-int
python tools/vgm_analyze.py "reference/vgz/sonic_1/02 - Green Hill Zone.vgz" --frames --chip all --channel FM1 PSG1

# Is every note right?  Symbolic, no rendering, self-aligning, exit 1 on a wrong/missing note.  Run this FIRST.
# "inst 8: synth_root is 1 octave too high (243 of 243 notes)" = fix that synth_root; "mixed" = a note problem.
# vgm_compare.py prints the same verdict ("Pitch verdict"); its per-note vgm_c / mod_c columns are audio
# cross-checks that still disagree on grace notes (todo item 3).
python tools/vgm_pitch_audit.py configs/sonic_1/02_green_hill_zone.yaml "reference/vgz/sonic_1/02 - Green Hill Zone.vgz" --list

# The song as read (asm or ROM) against its rip, lifted (docs/todo/vgz_conversion.md Phase 1): differences per
# channel and aspect, repeated ones grouped.  The lift takes the song's tempo; aspects default to what it reads
# (onset length note; --aspects all).  Pairs by number or the rips.yaml beside the configs
python tools/vgm_lift.py "reference/vgz/sonic_1/02 - Green Hill Zone.vgz"
python tools/vgm_lift.py --all --aspects onset           # every rip, a line each (~4 s warm)
python tools/vgm_lift.py --all --aspects onset length note --channels FM   # the FM note bytes and durations
python tools/vgm_lift.py configs/moonwalker/88_round_clear.yaml --skip DAC   # a ROM song and its rip
python tools/vgm_lift.py --all --configs configs/moonwalker                  # every Moonwalker pair
python tools/vgm_lift.py --all --configs configs/streets_of_rage   # its rips.yaml names their folder (folder:)
# No lift: every note's pitch (detune in), level and voice registers against the rip's frame log on
# its frame - what the lift cannot read.  Any driver; tied notes counted apart (vibrato runs on)
python tools/vgm_frames.py --all --configs configs/streets_of_rage
python tools/vgm_frames.py configs/golden_axe/89_the_battle.yaml           # one song: its misses

# Audit a conversion against its VGZ: per-note pitch/level, pitch verdict, channel balance, onset timing,
# vibrato rate/depth on long FM and PSG notes, noise spectrum, DAC rate.  Needs VGMPlay 0.51.x unzipped into
# reference/vgz/vgmplay/ (untracked, like the VGZ rips; or --vgmplay DIR / VGMPLAY_DIR) and an
# ffmpeg build with libopenmpt — setup in docs/pipeline.md § Verifying against a VGZ.
# Renders go to output/compare/<config>/; --skip-render reuses them.  Reference renders run in parallel and are
# kept in samples.render_cache (output/cache/vgmplay/), keyed on the VGZ + VGMPlay.ini: a song is rendered once
python tools/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz"
# Its "Per-instrument level error" table is what sample_list volumes are set from; --write-volumes applies
# the suggestions to the config (then re-convert and re-run to verify)
python tools/vgm_compare.py configs/sonic_1/02_green_hill_zone.yaml "reference/vgz/sonic_1/02 - Green Hill Zone.vgz" --write-volumes
# CI-style: JSON results + exit 1 when a threshold is exceeded (also --fail-unmatched N)
python tools/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz" --json output/compare/title.json --fail-balance-db 2 --fail-pitch-cents 25

# Every song at once (cores-1 in parallel): convert, one --write-volumes pass, re-convert, verify; prints the
# volumes changed, what is still >= 1 dB off (ceiling / channels-disagree / 2-note ones marked) and the pitch
# verdict per song.  Run after any change to how samples are rendered.  One write pass only: the errors are
# relative to the song's median note, so a further pass drifts the whole song.  Reference renders are reused.
python tools/measure_volumes.py
python tools/measure_volumes.py --only green_hill special_stage --no-write
# Other games: pairs from the rips.yaml beside the configs (config stem: rip; or --rips FILE)
python tools/measure_volumes.py --configs configs/moonwalker      # the rips: its mirror, reference/vgz/moonwalker
```


## Regression Testing

Cases are data: **`tests/cases.yaml`**, by game.  Sonic stays complete (every song, its lofi variants,
each config's `<name>_merged` build, four songs read from the ROM: `<name>_rom`); every other
driver has a few, chosen by line coverage.  A song joins only for code no case runs yet.
A case whose ROM is not in `input/roms/` is left out.
A converter change is safe only once every case it can move still produces a byte-identical MOD —
cells **and** the sample table and data (length, volume, finetune, loop, MD5).

**Selection by what each case ran** (`tests/selection.py`): `--generate-baselines` records the
project files each case executes (coverage.py, `pip install coverage`; render caches off, so
generating is cold: ~30 s).  A plain run diffs the working tree with each case's baseline commit
and runs only the cases a change can move: a driver's folder moves its game's cases, shared code
(`core/smps`, `core/convert`, ...) moves every case that imports it.  `--all` runs everything.

Every case converts with **`tests/settings.yaml`** (`convert.py --settings`), never
`configs/settings.yaml`, so tuning a song by ear does not move the baselines.  It states every key
the live file has (the runner exits 2 otherwise; add a new setting to both).
`tests/baselines/manifest.yaml` records each baseline's commit, date and the hashes of its settings
and config; `coverage.yaml` what each case ran.

```bash
python tests/regression.py --generate-baselines        # BEFORE a change, while the code is known-good (commit first)
python tests/regression.py                             # AFTER: the cases the change can move
python tests/regression.py --all                       # every case
python tests/regression.py --only title_screen moonwalker            # cases or groups
python tests/regression.py --generate-baselines --only title_screen   # accept one song's change
python -m pytest tests -q                              # unit tests (merge rules, detune, vgm, rom, ...)
python -m pytest tests/core/merge -q                   # one package's unit tests
```

**Workflow for any converter change:**
1. Commit, then run `--generate-baselines` while code is known-good (a `-dirty` record selects
   more than it needs to).
2. Make the change.
3. Run without flags — PASS means no regressions on channels, and no note the player cannot
   sound that the baseline sounds (`tools/mod_lint.py`: a `3xx` with no sample playing or a
   played-out one, a note on an empty instrument slot; run it on any MOD by hand too).
4. A FAIL is not an acceptance: read each changed cell before regenerating that song's baseline —
   a `3FF` where the sounding sample cannot reach the pitch, or a note on a slot the merged build
   stopped rendering, diffs like any intended change.

**The tools and readers have their own suite, `tests/tool_regression.py`**, selected the same way:
`vgm_analyze` on all 19 VGZs, `vgm_pitch_audit` on every baseline MOD, `vgm_lift` (the Moonwalker
pairs too), `frames_<game>` (`vgm_frames` on Golden Axe's and Streets of Rage's pairs) and `read_<game>` — `tools/song_dump.py`: every song of each game (Sonic's asm and ROM,
Moonwalker, Golden Axe, Streets of Rage) as read, walked and played, no rendering — byte for byte;
`--with-renders` adds `vgm_compare`.

```bash
python tests/tool_regression.py                          # PASS / FAIL + diff (a read_ case names the songs that differ)
python tests/tool_regression.py --all --with-renders     # everything, vgm_compare too
python tests/tool_regression.py --generate-baselines --only read_golden_axe   # accept one change
```

**Adding a case:** a line in `tests/cases.yaml` (`name`, `config`, `variant`, `why`); its
`--generate-baselines --only <name>`.  To ignore a channel while deliberately changing it, add
`"my_song": [8]` to `_CASE_OVERRIDES` in `tests/regression.py` (0-based MOD indices); it is normally
empty.  `tools/mod_compare.py` diffs any two MODs (`compare_mods(a, b, ignore_channels=[8])`).

## Rules that are easy to get wrong

One line each; the linked section has the cause and the detail.

**Reading the song** (`docs/smps_format.md`, `docs/smps_driver.md`)
- **Don't simulate bugs.**  Where a song's data or its driver misbehaves (a note past a frequency
  table reads stray code bytes, an overflow, a data error), convert what was meant, not the
  glitch: data fixes on, a past-table PSG note on the plausible continuation.  A rip that shows the
  glitch is evidence of the bug, not a target to match.
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

- A song carries its driver's `PlaybackRules` (tables, envelopes, drum names, timing): read
  `song.rules`, never a driver's table directly; nothing below `core/drivers` falls back to Sonic 1's.

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
