# sonic2mod — SMPS-to-MOD Converter

## Git Commits
Never include "Co-Authored-By" trailers in commit messages.

Converts Sonic 1 SMPS assembly music files to Amiga MOD format.

## Documentation Index

| Document | Contents |
|----------|----------|
| `docs/smps_driver.md` | **Sonic 1 driver reference** — all coord flag bytes ($E0–$F9), smpsDetune vs smpsChangeTransposition, timing system, smpsModSet, smpsNoteFill, FM operator order, DAC, PSG |
| `docs/pipeline.md` | **Conversion pipeline** — SMPS→MOD effect mapping (full table), tick/row math, effect priority, voice_map routing decision tree, BPM derivation, common gotchas, **VGZ verification setup** (VGMPlay location, `vgm_compare.py` report sections, `--json` / `--fail-*`) |
| `docs/fm_synthesis.md` | **YM2612 synthesis pipeline** — root/synth_root/target_rate explained, all settings, normalization, headroom/carrier balance, OPN2 internals, API reference, common mistakes |
| `docs/psg_synthesis.md` | **SN76489 PSG synthesis pipeline** — psg_map/psg_voice_map schema, envelope tables, root/synth_root, normalization, API |
| `docs/sfx_rendering.md` | **SFX→WAV offline driver** — tick loop, driver frequency tables, modulation halving, retrigger semantics, mix levels, hardware deviations |
| `docs/smps_format.md` | Assembly format syntax — header macros, dc.b token types, all effect macros |
| `docs/yaml_config.md` | Full YAML schema — all config fields, voice_map, sample_list, BPM formula |
| `docs/architecture.md` | **Module descriptions and layering** — `core/` is the bottom layer, `DriverState`, IR data classes, parser stages, ModFile layout |
| `docs/mod_effects.txt` | ProTracker MOD effect reference |
| `docs/audits/00_soundtrack_survey.md` | **All 18 configs vs their VGZs** (2026-09) — 10 samples found synthesised in the wrong octave (fixed), every `sample_list` volume set from measurement, what each song still needs |
| `docs/audits/02_ghz_audit.md` | **GHZ accuracy audit vs VGZ** (2026-09) — 867/867 notes, parser flag-ordering bug, `smpsAlterVol` law / TL level errors per instrument, grace notes, FM octave-convention trap |
| `docs/audits/09_remaining_audits.md` | **Robotnik … Game Over audits vs VGZ** (2026-09) — Stage Clear, Ending, Invincibility, Continue converted to `range_space: chip` (shared entries across pitch_offsets, key changes); Ending's PSG2 needs its own instrument; channel-RMS vs per-note disagreement explained as envelope decay |
| `docs/audits/08_special_stage_audit.md` | **Special Stage audit vs VGZ** (2026-09) — clean (507/507, ±0.5 dB); FM6 beat 4.45 Hz at the sample's own pitch and 5.6–6.7 Hz resampled — the item 7 example |
| `docs/audits/07_sbz_audit.md` | **Scrap Brain audit vs VGZ** (2026-09) — PSG2 instrument an octave high (all its notes under 60 ms), PSG3 envelope variants as noise entries (the rule the converter now follows), FM4 detune scoops; 1213/1213 at 60 ms after |
| `docs/audits/06_slz_audit.md` | **Star Light Zone audit vs VGZ** (2026-09) — FM2's bass walked down by `smpsAlterPitch` against a source-byte `root` → `range_space: chip` (how to convert a config), voice $05's +51 transposition; 819/819 after |
| `docs/audits/05_lz_audit.md` | **Labyrinth Zone audit vs VGZ** (2026-09) — PSG instrument an octave high (hidden from the audit by per-frame envelope writes), PSG `root`+`low` anchor vs `smpsAlterPitch` → rootless entry with channel `transpose`; 405/405 after |
| `docs/audits/04_syz_audit.md` | **Spring Yard audit vs VGZ** (2026-09) — 374/380, the six left are notes written below the PSG table (the driver reads code bytes: indices 125–127 measured), song-start key-on artefacts, channel-RMS vs per-note disagreement on PSG1 |
| `docs/audits/03_mz_audit.md` | **Marble Zone audit vs VGZ** (2026-09) — 731/731 notes, every FM/PSG channel within 0.7 dB, pitched rate-3 noise follows the melody by playback speed, snare volume, DAC-rate check by PCM write rate |
| `docs/audits/01_title_screen_audit.md` | **Accuracy audit vs VGZ** (2026-09) — method, per-channel numbers, config fixes, pending converter work (note fill frames, vibrato formula, EDx delay, volume baking) |
| `reference/Nuked-OPN2/` | Cycle-accurate YM2612/YM3438 C emulator |
| `reference/mml2mod-master/` | Reference MML-to-MOD converter |

## Project Structure

```
sonic2mod/
  convert.py         # CLI entry point — conversion
  analyze.py         # CLI entry point — Rich-formatted song analysis
  sonic2wav.py       # CLI entry point — SFX → WAV rendering
  core/              # Library package — the bottom layer; imports nothing from sfx/ or the chip packages
    tables.py        #   Note lookup tables, SMPS↔MOD note mapping, synth_note_name()
    driver_tables.py #   Sonic 1 driver transcription: FM/PSG frequency tables, note indices,
                     #   PSG envelopes, SMPS_OP_TO_REG_OFFSET, carrier/channel/pan maps
                     #   (sfx/tables.py re-exports this; it used to live there)
    driver_state.py  #   DriverState — the SMPS track state machine (level, pan, transpose, FM voice,
                     #   PSG entry) shared by the converter, its pre-passes and the config tools;
                     #   also source_names/source_map, chip_pitch, pan_is_hard, psg_range_entry
    levels.py        #   Chip level laws: TL 0.75 dB/step, attenuation 2 dB/step, pan law, dB→volume
    mod.py           #   MOD file writer (adapted from mml2mod-master) + cell/effect-slot helpers
    smps_parser.py   #   SMPS assembly parser → intermediate representation
    config.py        #   Per-song conversion config, YAML loading
    smps2mod.py      #   Conversion engine (IR → MOD)
    analysis.py      #   Analysis data model + analyze_song()
    cli.py           #   Shared Rich chrome for the three CLIs (branding, label column, UTF-8 stdout)
    cbuild.py        #   CLibrary — the gcc/MSVC compile + mtime cache both chip packages build with
    pcm.py           #   Mono/int8/raw16 helpers shared by the two synthesis pipelines
    version.py       #   get_version(): pyproject.toml is the one place the version is written (installed metadata is only a fallback)
  configs/           # YAML config files per song
  configs/settings.yaml  # Global synthesis settings
  output/            # Generated .mod files
  ym2612/            # YM2612 sample synthesis package (all segments complete)
    build.py         #   Auto-compiles ym3438.c → ym2612/ym3438.dll (spec for core.cbuild)
    wrapper.py       #   ctypes OPN2 class — write_reg, key_on/off, render_samples, render_mono; box_downsample
    ym3438_batch.c   #   C batch helpers: OPN2_RenderBatch(Mono), PCM_BoxDownsample (same arithmetic as the Python loops)
    voice.py         #   SmpsVoice → YM2612 register writes (program_voice)
    renderer.py      #   SmpsVoice + mod_note_index → 8-bit PCM (render_note)
    sample_generator.py #  voice_map → {inst: (pcm, rate)} dict (generate_fm_samples); one thread per instrument (`threads` setting)
    validate.py      #   Standalone test: python ym2612/validate.py (also checks the C helpers against the Python definitions)
  sn76489/            # SN76489 PSG sample synthesis package (all segments complete)
    build.py          #   Auto-compiles sn76489.c → sn76489/sn76489.dll (spec for core.cbuild)
    wrapper.py        #   ctypes SN76489 class — write_tone_freq/volume/noise, render_samples
    renderer.py       #   mod_note_index + noise config → 8-bit PCM (render_psg_tone/noise)
    sample_generator.py #  psg_map → {inst: (pcm, rate)} dict (generate_psg_samples)
    validate.py       #   Standalone test: python sn76489/validate.py
  sfx/                # Offline SMPS SFX driver → WAV (all segments complete)
    tables.py         #   Re-exports core/driver_tables.py under the name the SFX driver uses
    track.py          #   SfxTrack — mirrors the SMPS_Track RAM struct
    chips.py          #   Register writes mirroring SetVoice/SendVoiceTL/FMUpdateFreq/PSGUpdateFreq
    driver.py         #   SfxDriver — per-tick state machine (60 Hz, one tick per V-int)
    render.py         #   Frame loop, FM+PSG mix at 53267 Hz, tail detection
    resample.py       #   Polyphase windowed-sinc 53267 → 44100
    wav.py            #   16-bit stereo WAV writer (stdlib wave)
    amiga.py          #   8-bit Paula export — period grid, DC blocker, FFT rate pick, dither
    batch.py          #   Discovery, naming, global normalisation, 8-bit export
    validate.py       #   Standalone test: python sfx/validate.py
  docs/              # Technical documentation
  tools/             # Debug / analysis utilities
    vgm_analyze.py      #   FM + PSG pitch analyzer for VGM/VGZ files (+ rate-3 noise divider, DAC seeks)
    vgm_compare.py      #   Rendered per-channel MOD-vs-VGZ audit (VGMPlay + ffmpeg/libopenmpt)
    vgm_pitch_audit.py  #   Symbolic pitch audit: chip frequency registers vs the pitch each MOD note sounds at
    mod_compare.py      #   MOD binary parser + channel-by-channel comparator
    regression_test.py  #   Before/after regression test runner
    make_credits_config.py  # Regenerates configs/13_credits.yaml from the song (chip-pitch ranges, 31-instrument fold)
    config_to_chip_space.py # Converts a config's source-byte ranges to chip pitches (range_space: chip); warns where a range needs its own instrument
  sonic_1/           # Sonic 1 source files (driver asm, music, DAC samples)
```

## Setup

```bash
pip install pyyaml rich   # external dependencies
pip install ruff pyright  # lint/type checking (optional)
```

## Linting

```bash
ruff check .   # style + lint
pyright        # type checking
```

## Quick Usage

```bash
# Convert using YAML config (primary usage)
python convert.py configs/01_title_screen.yaml

# Override output path
python convert.py configs/01_title_screen.yaml --output output/title_screen.mod

# Render all 49 sound effects to 16-bit stereo WAV (no config needed)
python sonic2wav.py --all
python sonic2wav.py --all --dry-run          # parse + render + report, write nothing
python sonic2wav.py "sonic_1/sfx/SndB5 - Ring.asm"
python sfx/validate.py                       # tables, resampler, 8-bit chain, ticks, panning

# Export signed 8-bit mono .raw + manifest.yaml for Amiga/Paula → output/sfx8/
python sonic2wav.py --all --8bit
python sonic2wav.py --all --8bit --max-rate 16574   # A500 target, ~half the size
python sonic2wav.py --all --8bit --flat-rate 8287   # one rate for every sample

# Analyse a song (no config needed)
python analyze.py "sonic_1/music/Mus8A - Title Screen.asm"

# Analyse with config coverage diff
python analyze.py "sonic_1/music/Mus8A - Title Screen.asm" --config configs/01_title_screen.yaml

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

# Is every note right?  Symbolic, no rendering, self-aligning, exit 1 on a wrong/missing note.  Run this FIRST.
# "inst 8: synth_root is 1 octave too high (243 of 243 notes)" = fix that synth_root; "mixed" = a note problem.
# vgm_compare.py prints the same verdict ("Pitch verdict"); its per-note vgm_c / mod_c columns are audio
# cross-checks that still disagree on grace notes (todo item 3).
python tools/vgm_pitch_audit.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz" --list

# Audit a conversion against its VGZ: per-note pitch/level, pitch verdict, channel balance, onset timing,
# vibrato rate/depth on long FM and PSG notes, noise spectrum, DAC rate.  Needs VGMPlay 0.51.x unzipped into
# reference/vgz/vgmplay/ (untracked, like the VGZ rips; or --vgmplay DIR / VGMPLAY_DIR) and an
# ffmpeg build with libopenmpt — setup in docs/pipeline.md § Verifying against a VGZ.
# Renders go to output/compare/<config>/; --skip-render reuses them.
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz"
# Its "Per-instrument level error" table is what sample_list volumes are set from; --write-volumes applies
# the suggestions to the config (then re-convert and re-run to verify)
python tools/vgm_compare.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz" --write-volumes
# CI-style: JSON results + exit 1 when a threshold is exceeded (also --fail-unmatched N)
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz" --json output/compare/title.json --fail-balance-db 2 --fail-pitch-cents 25
```

## Regression Testing

Baselines live in `tests/baselines/`.  All 19 song configs are test cases — a converter change
is only safe once every one of them still produces a byte-identical MOD.  The conversions run
as parallel subprocesses (one per CPU by default; the whole suite takes a few seconds).

```bash
# BEFORE implementing a fix — save current output as baseline:
python tools/regression_test.py --generate-baselines

# AFTER implementing a fix — diff all channels that should remain same against baseline
python tools/regression_test.py

# Accept an intended change in ONE song without rewriting the other baselines
python tools/regression_test.py --generate-baselines --only title_screen

# Limit parallelism (e.g. when reading a failing conversion's output); -j 1 runs them one at a time
python tools/regression_test.py --jobs 4
```

**Workflow for any converter change:**
1. Run `--generate-baselines` while code is known-good.
2. Make the change.
3. Run without flags — PASS means no regressions on channels.

**Adding a new test case:** append a row to `_SONGS` in `tools/regression_test.py`
(`TEST_CASES` is built from it):
```python
("20_my_song", "my_song", "my_song", "My Song — what makes it worth testing"),
#  config stem   test name  baseline stem  description
```
To ignore a channel while deliberately changing it, add `"my_song": [8]` to `_CASE_OVERRIDES`
(0-based MOD indices); it is normally empty.

**`tools/mod_compare.py`** can be used standalone to diff any two MOD files:
```python
from tools.mod_compare import compare_mods, parse_mod
diffs = compare_mods("output/a.mod", "output/b.mod", ignore_channels=[8])
```

## Pipeline

`SmpsParser.parse_file()` → `SmpsSong` → `SmpsToModConverter.convert()` → `ModFile` → `.mod`

See `docs/pipeline.md` for the full data flow and conversion decisions.

## Key Conventions

- SMPS note range: 8 octaves (C0–B7), byte values $81–$DF
- MOD note range: 3 octaves (C1–B3), 36 semitones
- FM pitch names are real pitches everywhere: an SMPS FM label (+ pitch_offset + transposition) is the
  chip's note (`nA4` at offset 0 = 440 Hz), `synth_root: A4` renders 440 Hz, and `vgm_analyze.py`
  prints the same names. YM2612: `f = fnum × (clock/144) × 2^block / 2^21` (A4 = fnum 1083, block 4)
- Default FM transpose: -36 semitones (maps SMPS octaves 3–5 → MOD C1–B3)
- Duration persistence: last explicit `dc.b` duration carries to subsequent notes
- Labels emit no bytes: if one sits between a note byte and its duration byte, the duration still
  binds to that note (`SmpsParser._label_precedes_duration`). Affects 2 SFX, 0 music files
- Coordination flags DO complete a pending note: a note byte with no duration byte plays with the
  saved duration, and any flag / `smpsCall` / `smpsReturn` after it applies from the NEXT note
  (`FMDoNext` puts the non-duration byte back)
- `SmpsNote.is_retrigger` marks notes synthesised from a standalone duration byte — the driver's
  `.gotduration` path skips `FMSetFreq`, so those re-key at the **existing** frequency
- SFX headers (`smpsHeaderTempoSFX`/`ChanSFX`/`SFXChannel`) set `SmpsSongHeader.is_sfx` and
  `SmpsChannelHeader.hw_channel`; SFX run 1 tick per V-int with no tempo modifier
- Standalone duration bytes in `dc.b` **retrigger the last note** by default — without preceding `smpsNoAttack`: `SmpsNote(note_value=last_note_value, is_rest=False)`; with `smpsNoAttack` pending: rest/sustain `(is_rest=True, is_no_attack=True)`
- One state machine decides what a note plays: `core/driver_state.py`'s `DriverState` tracks the
  level, pan, driver transpose, FM voice and active PSG entry.  `_convert_channel`, the
  `_plan_levels` pre-passes, `_derive_rate3_dividers` and the two config tools all walk with it,
  so they cannot disagree.  Only MOD-emission state (note fill, vibrato, cursor) is the
  converter's own.  `core/analysis.py` deliberately keeps its own loop — it describes the song
  with no config in hand
- Parser continues past label boundaries — only stops at `smpsStop`/`smpsJump`
- Loop unrolling uses `stop_line` parameter to prevent re-entry into `smpsLoop`
- `_extend_looping_channels` replays the events AFTER the jump label (`SmpsChannel.label_event_index`),
  not every event at the label's tick — a flag written just before the label is not part of the loop
- YAML config requires `pyyaml` (`pip install pyyaml`)

## SMPS Effect → MOD Effect Mapping

Full table with gotchas in `docs/pipeline.md`. Quick reference:

| SMPS | Byte | MOD | Notes |
|------|------|-----|-------|
| `smpsAlterVol` | $E6 | `Cxx` | FM TL offset (0.75 dB/step); `Cxx` only where a note's level differs from its instrument's baked level |
| `smpsModSet` | $F0 | `4xy` | Vibrato; x from the cycle `2·speed·(steps+1)` frames, y per note from `delta·steps/2` over the note's FNUM / PSG divider |
| `smpsModOn` | $F1 | `4xy` | Re-activates stored mod params |
| `smpsModOff` | $F4 | (clear) | No MOD output |
| `smpsNoteFill` | $E8 | `ECx`/`C00` | Note cut; fill is in **frames** → scaled `(mod−1)/mod` to ticks, placed to the MOD tick |
| `smpsJump` | $F6 | `Bxx` | Position jump; first occurrence only |
| `smpsSetvoice` | $EF | (routing) | Updates voice_map instrument lookup |
| `smpsChangeTransposition` | $E9 | (pitch) | Adds to total_transpose |
| `smpsDetune`/`smpsAlterNote` | $E1 | **none** | FNUM offset (~10 cents); NOT semitones, NOT applied to pitch |
| `smpsPan` | $E0 | (level) | No MOD panning, but hard-panned FM notes count `fm_pan_law_db` (3 dB) quieter |
| `smpsLoop` | $F7 | (unrolled) | Loop replayed at parse time |
| `smpsCall` | $F8 | (inlined) | Subroutine events spliced inline |
| `smpsPSGAlterVol` | $EC | `Cxx` | SN76489 attenuation (2 dB/step); `Cxx` only where a note's attenuation differs from its instrument's baked one |
| `smpsPSGform` | $F3 | (routing) | Looks up `psg_map[byte]` → new PSG instrument |
| `smpsPSGvoice` | $F5 | (routing) | Looks up `psg_voice_map[label]` → new PSG instrument |
| `smpsSetTempoMod` | $EA | `Fxx` | Mid-song tempo change (Drowning, Credits): BPM scaled by the new tick rate, written on the change's row; fills / vibrato / `EDx` follow the new modifier |
| `smpsSetTempoDiv` | $EB | (re-timing) | Every track's duration divider from that tick (Credits' half-tempo passage): `_apply_global_tempo_div` re-times all channels; last write wins against a track's own `smpsChanTempoDiv` |
| `smpsChanTempoDiv` | $E5 | (durations) | This track's divider; applied at parse time and kept as an event so the re-timing above knows it |
| `smpsPSGform` | $F3 | (routing) | Noise mode is **permanent** (`cfSetPSGNoise` sets VoiceControl $E0); a later `smpsPSGvoice` only changes the envelope — honoured when its `psg_voice_map` entry is a noise type (Scrap Brain's `fTone_04`/`fTone_08` variants), ignored when it is a tone |
| `smpsNop` | $E2 | ignored | No MOD equivalent |

**Effect priority (one per row):** volume (Cxx) > vibrato (4xy) > note cut (ECx).  A note that starts
between rows takes `EDx` on the row it starts in when the slot is free (no `Cxx`, no cut inside the
attack row); it displaces an attack-row `4xy`.  Details: `docs/pipeline.md` § Notes that start between rows.

## Critical Gotchas

**Full gotchas with causes and fixes in `docs/pipeline.md`.**

1. **`smpsDetune`/`smpsAlterNote` ($E1) is NOT semitones** — it's a raw FNUM offset (~10 cents per unit). Does NOT affect `voice_map` range lookup or pitch placement. For chorus detune (FM5 vs FM4), use `channel_instrument_map` with a `finetune: 1` instrument variant.

2. **`root` is unconditional** — `smpsChangeTransposition` events do NOT affect the root path. Do NOT use `root` on channels that use `$E9` mid-song; use the `total_transpose` path instead (omit `root`, rely on YAML `transpose`).

3. **`smpsChangeTransposition` ($E9) is cumulative semitones** — each call adds to `SMPS_Track.Transpose`. Affects all subsequent notes and is included in `total_transpose`. This IS what shifts channels between register ranges in GHZ.

4. **Operator order** — SMPS binary stores OP4,OP3,OP2,OP1 (reversed). Correct mapping: `SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)` in `core/driver_tables.py`, shared by `ym2612/voice.py` and `sfx/chips.py` (one source of truth — it used to be written out in both). Wrong mapping → "overdriven guitar" distortion (OP1 carrier placed in self-feedback slot).

5. **Synthesis enabled by default** — `fm_synthesis.enabled: true` / `psg_synthesis.enabled: true` in `configs/settings.yaml`. Requires gcc/MSVC for ym3438.c / sn76489.c. Set `false` to use pre-rendered samples from `samples/` instead.

6. **FM5 falls through into FM1 data** — parser does not stop at label boundaries; FM5 typically lacks `smpsStop` and shares FM1's note data (intentional chorus/detune design).

7. **`smpsNoteFill` and `smpsModSet` wait/speed count V-int frames, not ticks** — `TempoWait` only delays `DurationTimeout`. `SmpsToModConverter._tpf(modifier)` = `(mod−1)/mod` converts them (use `_tpf_at(tick)` wherever the tick is known, so mid-song `smpsSetTempoMod` is honoured) (fill, wait, and the vibrato cycle). None is multiplied by the tempo divider. Cuts are placed to the MOD tick on whichever row they fall (`ECx` in-row, `C00` on a boundary); a fill that outlasts the note emits nothing. A fill equal to the duration byte DOES fire when the tempo modifier is > 1.

7a. **Driver ticks are unevenly spaced** — with tempo modifier *m*, `TempoWait` holds every *m*-th frame, so tick *k* falls on frame `k + k // (m−1)`. GHZ's odd ticks are 16.7 ms after the even ones, not 25 ms. `_note_cell` measures `EDx` delays in frames for that reason. Two note-ons never share a cell: a 1-tick grace note keeps its row and the note it slides into takes the next one. A `Cxx` due on a delayed note's attack row moves to the note's next row.

7b. **FM levels are "baked" (`fm_volume_scaling: baked`, `configs/settings.yaml`)** — per MOD instrument, the (TL offset, pan) level most of its notes play at needs no command and is what its `sample_list` volume means; other notes get `Cxx = volume × 10^(ΔdB/20)`. TL offset = `smpsHeaderFM` volume + `smpsAlterVol`; hard pan = −3 dB. No variant instruments. PSG works the same way (`psg_volume_scaling: baked`, attenuation 2 dB/step, no pan). Both laws live in `core/levels.py` and the baselines are planned by `SmpsToModConverter._plan_levels`, which walks the channels with the same `DriverState` the conversion does. When tuning a `sample_list` volume, all channels sharing the instrument should show the same error in `vgm_compare.py` — if they don't, it is not a volume problem. Details: `docs/pipeline.md` §FM levels.

7d. **PSG3 stays a noise channel once `smpsPSGform` ran** — `cfSetPSGNoise` writes VoiceControl $E0 and nothing in Sonic 1 music turns it back; `smpsPSGvoice` after it only picks the hi-hat's envelope. The converter used to switch to a tone instrument there (Credits PSG3); a noise-type `psg_voice_map` entry under the label is the envelope's variant and is honoured (Scrap Brain's `fTone_04`/`fTone_08`). A note transposed past the PSG table's ends plays whatever ROM follows the table; indices 125–127 are measured from the Spring Yard and Credits recordings (0 = inaudible, 922 = B2, 540 = G#3) and sit at the end of `PSG_FREQUENCIES_EXTENDED`, so `core.driver_tables.psg_index_semitone` gives the hardware's pitch there (`range_space: chip` reproduces it).

7c. **A MOD BPM is a whole number** — `auto_bpm` rounds; choose `target_speed` so the exact BPM is (nearly) integer (speed changes MOD ticks per row, not the row grid). `convert.py` prints the rounding error and the better speed; Special Stage at speed 3 ran 0.44 % slow. Details: `docs/pipeline.md` §BPM and speed setup.

8. **smpsModSet → `4xy`** — only the FIRST half-swing uses the halved step count (`lsr.b #1`); the counter reloads from the original byte, so the steady cycle is `2·speed·(steps+1)` frames and the swing is `delta·steps/2` units of the note's own FNUM (644 C … 1216 B) or PSG divider. `_vibrato_speed` / `_vibrato_depth` turn that into x and a per-note y; verified against six songs' VGZs. No config needs a `vibrato:` override any more. Details: `docs/pipeline.md` gotcha 4.

9. **`mod_pattern_breaks` call order is mandatory** — must be called AFTER `converter.convert()` (writes note data) and BEFORE `converter._set_loop_point()` (writes `Bxx`). Wrong order → loop target lands in wrong pattern. Break coordinate formula: `body_start = P*64 + break_row + 1`; flat rows before `body_start` use `flat//64 : flat%64`, rows after use pattern `P+1 + br//64 : br%64` where `br = flat_row - body_start`. If `target_row != 0`, also write `Dxx` (BCD row) on a free channel at the same row.

## voice_map (per-voice octave-range instrument routing)

Routes SMPS voice index + **source-note range** → MOD instrument + optional pitch anchor.
Ranges checked against `(note_value − $81)`. `smpsDetune`/`smpsAlterNote` is a raw FNUM offset
and does NOT affect range lookup.

- `low`/`high` — SMPS note names without `n` prefix (e.g. `G5`, `Gs6`, `C7`)
- `mod_instrument` — MOD instrument slot (1-based)
- `root` — **absolute** MOD note anchor; source `low` always plays here regardless of `smpsChangeTransposition` or pitch_offset
- `synth_root` — synthesis pitch override; `target_rate` is NOT adjusted — output pitch = synth_root's frequency
- `vibrato: XY` — per-entry vibrato override (speed X, depth Y; `0` = none); also works in `psg_map` / `psg_voice_map`. Not used by any shipped config — the computed `4xy` matches the hardware
- `range_space: chip` (song-level) — match `low`/`high` and anchor `root` on the **real pitch the chip plays** (byte + pitch_offset + `smpsChangeTransposition`; PSG through the driver table) instead of the source byte. Needed when a song changes key with `$E9` while keeping a voice (Credits: FM2 twenty times); then `synth_root` = `low` and a merged voice's entry keeps its own range. Source space stays the default
- Output formula: `root + (source − low)`, clamped C1–B3
- Rootless entries use channel-transpose path: `smps_note + total_transpose`
- When `voice_map` covers all notes for a channel, set `transpose: 0`

```yaml
voice_map:
  0:                        # voice index (from smpsSetvoice)
    - low:  G5              # SMPS source note — bottom of range
      high: G6
      mod_instrument: 4
      root: Fs2             # G5 plays at F#2; each semitone above shifts output up by 1
      synth_root: C6        # (optional) synthesize at C6 frequency instead of G5
      vibrato: 31           # (optional) override vibrato for this range (speed=3, depth=1)
    - low:  Gs6
      high: C7
      mod_instrument: 12
      root: G3
```

**When NOT to use `root`:** channels with mid-song `smpsChangeTransposition` ($E9) — root ignores total_transpose and will place notes incorrectly. Use `transpose` only and let `total_transpose` handle pitch. The same holds for a `psg_voice_map` entry with `low`: Labyrinth Zone's PSG1/PSG2 run `smpsAlterPitch` in a loop, so their entry is rootless (no `low`/`high`) with `transpose: -12` on the channels. Or convert the whole config with `python tools/config_to_chip_space.py configs/<song>.yaml` (Star Light, Stage Clear, Ending, Invincibility, Continue were) and audit.

## sample_list Entry Format

`[inst_num, "filename.raw", volume, finetune]` — loaded from `samples_dir`.
- `inst_num`: 1-based MOD instrument slot
- `volume`: 0–64
- `finetune`: -8..+7 (MOD finetune nibble; +1 ≈ +12.5 cents)
- Synthesis path: when synthesis is enabled, `sample_list` entries whose `inst_num` was synthesized apply `volume` and `finetune` overrides to the synthesized sample — the file is not loaded from disk

## YM2612 Synthesis

Full reference: `docs/fm_synthesis.md`.
Enable: set `fm_synthesis: {enabled: true}` in `configs/settings.yaml`.
Smoke tests: `python ym2612/validate.py` / `renderer.py` / `sample_generator.py`

## SN76489 PSG Synthesis

Full reference: `docs/psg_synthesis.md`.
Enable: set `psg_synthesis: {enabled: true}` in `configs/settings.yaml`.
Rate-3 noise (`noise_rate: 3`): set nothing else — the converter derives the tone-2 divider from the
song's own notes through the driver's `PSGFrequencies` table (`_derive_rate3_dividers`; `nMaxPSG` is
divider 0, clocked as N=1) and prints it. `tone2_n` / `synth_root` remain as overrides; `convert.py`
warns when a rate-3 `synth_root` is outside the driver table (`A8` gives a 7 kHz dull rattle).
Smoke tests: `python sn76489/validate.py` / `renderer.py` / `sample_generator.py`

## Testing

Regression baselines: `tests/baselines/title_screen_baseline.mod` and `tests/baselines/ghz_baseline.mod`.
Both are active test cases in `tools/regression_test.py` (Title Screen + GHZ Act 1).

Verified channel coverage:
- All 9 channels parsed (1 DAC, 5 FM, 3 PSG)
- FM5 fall-through into FM1 data works
- PSG3 loop unrolled correctly (5 iterations)
- Output is valid 10CH MOD, opens in Fast Tracker 2 Clone
