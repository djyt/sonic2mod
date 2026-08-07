# sonic2mod — SMPS-to-MOD Converter

## Git Commits
Never include "Co-Authored-By" trailers in commit messages.

Converts Sonic 1 SMPS assembly music files to Amiga MOD format.

## Documentation Index

| Document | Contents |
|----------|----------|
| `docs/smps_driver.md` | **Sonic 1 driver reference** — all coord flag bytes ($E0–$F9), smpsDetune vs smpsChangeTransposition, timing system, smpsModSet, smpsNoteFill, FM operator order, DAC, PSG |
| `docs/pipeline.md` | **Conversion pipeline** — SMPS→MOD effect mapping (full table), tick/row math, effect priority, voice_map routing decision tree, BPM derivation, common gotchas |
| `docs/fm_synthesis.md` | **YM2612 synthesis pipeline** — root/synth_root/target_rate explained, all settings, normalization, headroom/carrier balance, OPN2 internals, API reference, common mistakes |
| `docs/psg_synthesis.md` | **SN76489 PSG synthesis pipeline** — psg_map/psg_voice_map schema, envelope tables, root/synth_root, normalization, API |
| `docs/sfx_rendering.md` | **SFX→WAV offline driver** — tick loop, driver frequency tables, modulation halving, retrigger semantics, mix levels, hardware deviations |
| `docs/smps_format.md` | Assembly format syntax — header macros, dc.b token types, all effect macros |
| `docs/yaml_config.md` | Full YAML schema — all config fields, voice_map, sample_list, BPM formula |
| `docs/architecture.md` | Module descriptions — IR data classes, parser stages, ModFile layout |
| `docs/mod_effects.txt` | ProTracker MOD effect reference |
| `reference/Nuked-OPN2/` | Cycle-accurate YM2612/YM3438 C emulator |
| `reference/mml2mod-master/` | Reference MML-to-MOD converter |

## Project Structure

```
sonic2mod/
  convert.py         # CLI entry point — conversion
  analyze.py         # CLI entry point — Rich-formatted song analysis
  sonic2wav.py       # CLI entry point — SFX → WAV rendering
  core/              # Library package
    tables.py        #   Note lookup tables, SMPS↔MOD note mapping
    mod.py           #   MOD file writer (adapted from mml2mod-master)
    smps_parser.py   #   SMPS assembly parser → intermediate representation
    config.py        #   Per-song conversion config, YAML loading
    smps2mod.py      #   Conversion engine (IR → MOD)
    analysis.py      #   Analysis data model + analyze_song()
  configs/           # YAML config files per song
  configs/settings.yaml  # Global synthesis settings
  output/            # Generated .mod files
  ym2612/            # YM2612 sample synthesis package (all segments complete)
    build.py         #   Auto-compiles ym3438.c → ym2612/ym3438.dll (gcc or cl)
    wrapper.py       #   ctypes OPN2 class — write_reg, key_on/off, render_samples
    voice.py         #   SmpsVoice → YM2612 register writes (program_voice)
    renderer.py      #   SmpsVoice + mod_note_index → 8-bit PCM (render_note)
    sample_generator.py #  voice_map → {inst: (pcm, rate)} dict (generate_fm_samples)
    validate.py      #   Standalone test: python ym2612/validate.py
  sn76489/            # SN76489 PSG sample synthesis package (all segments complete)
    build.py          #   Auto-compiles sn76489.c → sn76489/sn76489.dll (gcc or cl)
    wrapper.py        #   ctypes SN76489 class — write_tone_freq/volume/noise, render_samples
    renderer.py       #   mod_note_index + noise config → 8-bit PCM (render_psg_tone/noise)
    sample_generator.py #  psg_map → {inst: (pcm, rate)} dict (generate_psg_samples)
    validate.py       #   Standalone test: python sn76489/validate.py
  sfx/                # Offline SMPS SFX driver → WAV (all segments complete)
    tables.py         #   Driver frequency tables, PSG envelopes, register/channel maps
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
    vgm_analyze.py      #   FM + PSG pitch analyzer for VGM/VGZ files
    mod_compare.py      #   MOD binary parser + channel-by-channel comparator
    regression_test.py  #   Before/after regression test runner
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
python tools/vgm_analyze.py "reference/vgm/01 - Title Theme.vgz" --chip fm --channel FM1 FM2
# Analyse SN76489 PSG noise channel (compare against title_screen.yaml output)
python tools/vgm_analyze.py "reference/vgm/01 - Title Theme.vgz" --chip psg --channel NOISE
# Show all chips / all channels
python tools/vgm_analyze.py "reference/vgm/01 - Title Theme.vgz" --chip all --max-rows 0
```

## Regression Testing

Baselines live in `tests/baselines/`. Test cases: GHZ and Title Screen

```bash
# BEFORE implementing a fix — save current output as baseline:
python tools/regression_test.py --generate-baselines

# AFTER implementing a fix — diff all channels that should remain same against baseline
python tools/regression_test.py
```

**Workflow for any converter change:**
1. Run `--generate-baselines` while code is known-good.
2. Make the change.
3. Run without flags — PASS means no regressions on channels.

**Adding a new test case:** append an entry to `TEST_CASES` in `tools/regression_test.py`:
```python
{
    "name": "my_song",
    "config": "configs/my_song.yaml",
    "baseline": "tests/baselines/my_song_baseline.mod",
    "ignore_channels": [],                   # normally empty; only set when deliberately
                                             # changing that channel (0-based MOD indices)
    "description": "My Song — all channels",
},
```

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
- Default FM transpose: -36 semitones (maps SMPS octaves 3–5 → MOD C1–B3)
- Duration persistence: last explicit `dc.b` duration carries to subsequent notes
- Labels emit no bytes: if one sits between a note byte and its duration byte, the duration still
  binds to that note (`SmpsParser._label_precedes_duration`). Affects 2 SFX, 0 music files
- `SmpsNote.is_retrigger` marks notes synthesised from a standalone duration byte — the driver's
  `.gotduration` path skips `FMSetFreq`, so those re-key at the **existing** frequency
- SFX headers (`smpsHeaderTempoSFX`/`ChanSFX`/`SFXChannel`) set `SmpsSongHeader.is_sfx` and
  `SmpsChannelHeader.hw_channel`; SFX run 1 tick per V-int with no tempo modifier
- Standalone duration bytes in `dc.b` **retrigger the last note** by default — without preceding `smpsNoAttack`: `SmpsNote(note_value=last_note_value, is_rest=False)`; with `smpsNoAttack` pending: rest/sustain `(is_rest=True, is_no_attack=True)`
- Parser continues past label boundaries — only stops at `smpsStop`/`smpsJump`
- Loop unrolling uses `stop_line` parameter to prevent re-entry into `smpsLoop`
- YAML config requires `pyyaml` (`pip install pyyaml`)

## SMPS Effect → MOD Effect Mapping

Full table with gotchas in `docs/pipeline.md`. Quick reference:

| SMPS | Byte | MOD | Notes |
|------|------|-----|-------|
| `smpsAlterVol` | $E6 | `Cxx` | Cumulative volume → set volume |
| `smpsModSet` | $F0 | `4xy` | Vibrato; steps halved in hardware |
| `smpsModOn` | $F1 | `4xy` | Re-activates stored mod params |
| `smpsModOff` | $F4 | (clear) | No MOD output |
| `smpsNoteFill` | $E8 | `ECx` | Note cut; only values 1–15 representable |
| `smpsJump` | $F6 | `Bxx` | Position jump; first occurrence only |
| `smpsSetvoice` | $EF | (routing) | Updates voice_map instrument lookup |
| `smpsChangeTransposition` | $E9 | (pitch) | Adds to total_transpose |
| `smpsDetune`/`smpsAlterNote` | $E1 | **none** | FNUM offset (~10 cents); NOT semitones, NOT applied to pitch |
| `smpsPan` | $E0 | ignored | MOD panning is channel-based |
| `smpsLoop` | $F7 | (unrolled) | Loop replayed at parse time |
| `smpsCall` | $F8 | (inlined) | Subroutine events spliced inline |
| `smpsPSGAlterVol` | $EC | `Cxx` | Same path as smpsAlterVol; delta adds to current_volume |
| `smpsPSGform` | $F3 | (routing) | Looks up `psg_map[byte]` → new PSG instrument |
| `smpsPSGvoice` | $F5 | (routing) | Looks up `psg_voice_map[label]` → new PSG instrument |
| `smpsNop` | $E2 | ignored | No MOD equivalent |

**Effect priority (one per row):** volume (Cxx) > vibrato (4xy) > note cut (ECx).

## Critical Gotchas

**Full gotchas with causes and fixes in `docs/pipeline.md`.**

1. **`smpsDetune`/`smpsAlterNote` ($E1) is NOT semitones** — it's a raw FNUM offset (~10 cents per unit). Does NOT affect `voice_map` range lookup or pitch placement. For chorus detune (FM5 vs FM4), use `channel_instrument_map` with a `finetune: 1` instrument variant.

2. **`root` is unconditional** — `smpsChangeTransposition` events do NOT affect the root path. Do NOT use `root` on channels that use `$E9` mid-song; use the `total_transpose` path instead (omit `root`, rely on YAML `transpose`).

3. **`smpsChangeTransposition` ($E9) is cumulative semitones** — each call adds to `SMPS_Track.Transpose`. Affects all subsequent notes and is included in `total_transpose`. This IS what shifts channels between register ranges in GHZ.

4. **Operator order** — SMPS binary stores OP4,OP3,OP2,OP1 (reversed). Correct mapping: `_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)`. Wrong mapping → "overdriven guitar" distortion (OP1 carrier placed in self-feedback slot).

5. **Synthesis enabled by default** — `fm_synthesis.enabled: true` / `psg_synthesis.enabled: true` in `configs/settings.yaml`. Requires gcc/MSVC for ym3438.c / sn76489.c. Set `false` to use pre-rendered samples from `samples/` instead.

6. **FM5 falls through into FM1 data** — parser does not stop at label boundaries; FM5 typically lacks `smpsStop` and shares FM1's note data (intentional chorus/detune design).

7. **`smpsNoteFill` > 15 ignored** — ECx has a 4-bit parameter; fill values 16+ cannot be represented and produce no MOD effect (note sustains naturally to full duration).

8. **smpsModSet step count halved in hardware** — driver does `lsr.b #1` before storing. Value 16 → 8 actual oscillation steps.

9. **`mod_pattern_breaks` call order is mandatory** — must be called AFTER `converter.convert()` (writes note data) and BEFORE `converter._set_loop_point()` (writes `Bxx`). Wrong order → loop target lands in wrong pattern. Break coordinate formula: `body_start = P*64 + break_row + 1`; flat rows before `body_start` use `flat//64 : flat%64`, rows after use pattern `P+1 + br//64 : br%64` where `br = flat_row - body_start`. If `target_row != 0`, also write `Dxx` (BCD row) on a free channel at the same row.

## voice_map (per-voice octave-range instrument routing)

Routes SMPS voice index + **source-note range** → MOD instrument + optional pitch anchor.
Ranges checked against `(note_value − $81)`. `smpsDetune`/`smpsAlterNote` is a raw FNUM offset
and does NOT affect range lookup.

- `low`/`high` — SMPS note names without `n` prefix (e.g. `G5`, `Gs6`, `C7`)
- `mod_instrument` — MOD instrument slot (1-based)
- `root` — **absolute** MOD note anchor; source `low` always plays here regardless of `smpsChangeTransposition` or pitch_offset
- `synth_root` — synthesis pitch override; `target_rate` is NOT adjusted — output pitch = synth_root's frequency
- `vibrato: XY` — per-entry vibrato override (speed X, depth Y); also works in `psg_map` / `psg_voice_map`
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

**When NOT to use `root`:** channels with mid-song `smpsChangeTransposition` ($E9) — root ignores total_transpose and will place notes incorrectly. Use `transpose` only and let `total_transpose` handle pitch.

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
Smoke tests: `python sn76489/validate.py` / `renderer.py` / `sample_generator.py`

## Testing

Regression baselines: `tests/baselines/title_screen_baseline.mod` and `tests/baselines/ghz_baseline.mod`.
Both are active test cases in `tools/regression_test.py` (Title Screen + GHZ Act 1).

Verified channel coverage:
- All 9 channels parsed (1 DAC, 5 FM, 3 PSG)
- FM5 fall-through into FM1 data works
- PSG3 loop unrolled correctly (5 iterations)
- Output is valid 10CH MOD, opens in Fast Tracker 2 Clone
