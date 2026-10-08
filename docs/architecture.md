# sonic2mod architecture

sonic2mod reads a Sonic 1 SMPS song (assembly, ROM bytecode or a VGM register log), builds an
intermediate representation (`SmpsSong`), walks it through a per-song config and writes an Amiga
ProTracker MOD, rendering its instruments on emulated YM2612 / SN76489 chips.

This page is about the code: how data flows, how the packages are layered and what each module
holds.  What the conversion decides is `docs/pipeline.md`; the config keys are
`docs/yaml_config.md`.

1. [Data flow](#1-data-flow)
2. [Layering](#2-layering)
3. [Reading a song](#3-reading-a-song-coresmps-coresource-corerom-corevgm)
4. [The config](#4-the-config-coreconfig)
5. [Planning: the song read through its config](#5-planning-coreplan)
6. [The converter](#6-the-converter-coreconvert)
7. [The merged build](#7-the-merged-build-coremerge)
8. [The MOD file](#8-the-mod-file-coremod)
9. [Chips, audio and sample rendering](#9-chips-audio-and-sample-rendering)
10. [Verification](#10-verification-coreaudit-tools-tests)
11. [Reporting and CLIs](#11-reporting-and-clis)

---

## 1. Data flow

### Reading: three sources, one IR

```
  .asm                     ROM + sound ID                 .vgm / .vgz rip
    │                           │                               │
 SmpsParser                read_rom_code                  frame_log → lift_song
 (smps/parser.py)          (rom/song.py)                  (vgm/, vgm/lift/)
    │                           │                               │
    └──────── SmpsCode ─────────┘                               │
                 │                                              │
           song_from_code  (smps/code.py: the driver's          │
                 │          reading rules, shared)              │
                 └──────────────────┬───────────────────────────┘
                                    ▼
                                SmpsSong        read_song(path) picks the front end
```

### Converting: one MOD channel per SMPS channel

```
 SmpsSong ─┐
           ├─ SmpsToModConverter.convert() ─→ ModFile ─→ .mod
 config ───┘        │
                    ├─ plan:    DriverState walk → which instrument, which MOD note, which level
                    ├─ render:  instrument catalogue → FM / PSG generators → samples
                    └─ write:   ChannelWriter per channel → cells; ModLayout → tempo, loop, end
```

### Rendering a sample

```
 config maps ─→ instrument catalogue ─→ generate_fm_samples  (ym2612/)  ─┐
 (voice_map,     (plan/instruments.py)   generate_psg_samples (sn76489/) ─┴→ {slot: (pcm, rate)}
  psg maps)                                                               └→ loop, release rate
```

The merged build (§ 7) adds a merge plan between planning and writing: it folds channels and
adds composite instruments to the same catalogue.

---

## 2. Layering

```
 convert.py  analyze.py  sonic2wav.py            CLIs (thin)
 tools/                                          analysis and audit utilities
 sfx/                                            the offline SFX driver
 ym2612/  sn76489/                               chip wrappers + sample generators
 ─────────────────────────────────────────────── core/ imports none of the above
 core/  ui
        convert   audit
        merge
        plan
        config
        source
        vgm   rom
        smps  mod
        chips  audio  render_cache  files        diagnostics.py: importable from anywhere
```

- Inside `core/` a package imports only packages below it.  `smps` imports `chips` only; `audio`,
  `chips`, `files` and `diagnostics` import nothing in `core`; `render_cache` only `files`.
- A package's `__init__.py` exports what other packages import (`from ..smps import SmpsSong`);
  its own modules import each other directly.  A module can move inside its package unseen.
- `core/` never imports `ym2612/`, `sn76489/` or `sfx/`.  The converter receives the two sample
  generators from `convert.py` as `SampleGenerators(fm=…, psg=…)`
  (`core/convert/generators.py`); a converter given none raises if asked to synthesise.
- `ym2612/` and `sn76489/` import `core` (plan, config, smps, chips, mod, audio, cbuild,
  render_cache), never each other.  `sfx/` imports `core` and both chip wrappers.

---

## 3. Reading a song (`core/smps`, `core/source`, `core/rom`, `core/vgm`)

### The IR (`core/smps/song.py`)

| Class | Holds |
|---|---|
| `SmpsSong` | `header`, `channels`, `voices`, `psg_envelopes`, `fm_frequencies` (the FM table notes play from: Sonic 1's, or a ROM driver's), `dropped` (flags a ROM driver read and left out); `end_tick()`, `loop_target_tick()` |
| `SmpsSongHeader` | voice label, FM / PSG counts, tempo divider and modifier, channel headers, `is_sfx` |
| `SmpsChannelHeader` | type, label, pitch offset, volume, PSG modulation byte, voice and `psg_voice_label`; SFX `hw_channel`; `chip_channel` where the driver's order is not header order (`source_names`) |
| `SmpsChannel` | header, `events`, jump / loop info (`loop_tick`, `loop_event_index`, `loop_label`) |
| `SmpsEvent` | a note or an effect, with its tick |
| `SmpsNote` | value, duration, rest / no-attack / retrigger flags |
| `SmpsEffect` | a `CoordFlag` (keyed by Sonic 1's byte; another driver's flag past `$FF`: `SET_VOL`) and its parameters |
| `SmpsVoice` | algorithm, feedback, `operators` (each `VoiceField` as four ints in driver order), `pan` (a voice that stores B4: the walk pans its track on smpsSetvoice); `registers(tl_offset)` |

### Front ends

| Module | Role |
|---|---|
| `smps/parser.py` | `SmpsParser`: asm → `SmpsCode` → `SmpsSong`.  `_preprocess` (comments, `FixMusicAndSFXDataBugs` conditionals), `_collect_labels`, `_parse_header`, `_parse_voices`, then each line to `Op`s (label, byte, flag, call, return, loop, jump, stop).  `fix_data_bugs=True` by default |
| `smps/code.py` | `SmpsCode`, `Op`, `SongCode`; `song_from_code`: walks each channel through the ops with the driver's reading rules (pending durations, standalone durations, `smpsNoAttack`, loops unrolled, calls inlined, fall-through, each channel's own loop point).  Asm and ROM share it |
| `rom/` | Generic readers driven by an `SmpsVariant` (`variant.py`: what one driver differs by - memory, locate, flags, header and voice layouts, envelope commands, DAC; `flags.py`; `memory.py`: `SoundMemory`, how a driver reads pointers).  `header.py`, `tracks.py` (bytes → `SmpsCode`), `voices.py`, `envelopes.py`; `fixes.py` (`RomFix`, `apply_fixes`).  `z80.py` (`z80_ram`: Z80 RAM as the 68k's copy loops fill it).  Families: `smps68k/` (`sonic1.py` with rev01's data fixes, `type1a.py`, `common.py`, `memory.py`, `locate.py`: the `Go_` block by its tables' shape, `dac.py` + `kosinski.py`); `smpsz80/` (`type0fm.py`: Golden Axe's Type 0 FM; `layout.py`: its header and 26-byte voice; `drums.py`: its FM drum programs run frame by frame -> `FmDrum`; `memory.py`: the bank window; `locate.py`: the driver by its FM table, the bank by its sound header).  `variants.py` (registry, SHA-1 pins, `data_fixes`), `detect.py` (`detect_variant`), `song.py` (`locate_sounds`, `read_rom_code`, `read_rom_song`, `dac_samples`).  Layers: vocabulary ← readers ← families ← registry ← `detect` / `song` |
| `vgm/` | `reader.py` (`read_vgm` → `VgmLog`), `chipstate.py` (`ChipState.replay`: registers write by write), `frames.py` (`frame_log`: per V-int frame, every channel's state), `notes.py` (`note_starts`, `pitch_segments`), `cache.py` (`load_frames`, cached) |
| `vgm/lift/` | `lift_song(frames, LiftOptions)` → `SmpsSong`: `tracks.py` (hits by frame), `tempo.py` (`infer_tempo`; a given modifier is where the song starts), `song.py` (`LIFTED_ASPECTS`: what a lift states).  Work in progress: `docs/todo/vgz_conversion.md` |
| `source/load.py` | `read_song(path, options, rom_song, driver, fix_data_bugs)`: picks the front end by suffix; `read_dac(path)` |
| `smps/asm_writer.py` | `write_asm`: a `SongCode` back to SMPS2ASM text the parser reads into the same song |

How the asm is spelled and how the parser reads it: `docs/smps_format.md`.

### What the driver does with it (`core/smps`)

| Module | Role |
|---|---|
| `driver_tables.py` | Transcription of the Sonic 1 driver: FM / PSG frequency tables, `fm_note_index`, `psg_note_index`, `chip_pitch`, `psg_tone2_divider`, PSG envelopes (`SONIC1_ENVELOPES`), `SMPS_OP_TO_REG_OFFSET`, pan values, `SmpsDriver`.  Self-checks at import |
| `names.py` | Note labels and the two spellings (`semitone_to_note_name`: driver's `Es`; `synth_note_name`: config's `F`), `parse_smps_note`, `parse_synth_note`, DAC names, flag macro names, `source_names` |
| `song_prep.py` | The song as played: `apply_global_tempo_div` (`smpsSetTempoDiv` re-timing), `extend_looping_channels` |
| `run_out.py` | `apply_run_out`: a driver's key-on run-out (Type 0 FM: 256 frames) as the walk's last step - the held note cut, a rest after |
| `percussion.py` | `FmDrum`, `FmFrame`: a drum track's FM drum as the chip plays it, frame by frame (`SmpsSong.fm_drums`) |
| `tempo.py` | `TempoSegment`, `tempo_schedule`: the frame each tick is read on; `NO_TEMPO_HOLDS` (SFX, a driver's no-stall tempo) |
| `track.py` | `TrackState`: one track's driver state as its flags leave it |
| `playback.py` | `played_song`: each note as the driver plays it (`PlayedNote`), the asm's spelling gone |
| `compare.py` | `compare_songs`, `align_songs`, `parse_differences`: two songs note by note per `Aspect` (the lift's yardstick) |

---

## 4. The config (`core/config`)

| Module | Role |
|---|---|
| `loader.py` | `load_yaml` (refuses a key given twice), `read_yaml_file`, `apply_variant`, value words (`mode_word`, `dither_mode`) |
| `entries.py` | `InstrumentRange`, `PsgInstrumentEntry`, `ChannelConfig`, `DacSampleConfig`, `MergeGroup` and one parser per section |
| `song.py` | `ConversionConfig`: `from_yaml(path, variant)` → `from_data` (unknown top-level keys are errors), `read_song()`, `is_minimal`, `stated()`, `use_source()`, `mod_channel_count` |
| `settings.py` | `SampleSettings` and its subclasses `SynthesisSettings` (YM2612, `legato`, `player`) and `PsgSynthesisSettings`; `find_settings`, `load_settings`, `with_song_overrides` |
| `bpm.py` | `derive_bpm`, `exact_bpm`, `bpm_rounding_options` |

Every key: `docs/yaml_config.md`.  Settings are read by every CLI and tool; a constant in the
code is only the fallback when a key is absent.

---

## 5. Planning (`core/plan`)

The song read through its config.  **One state machine decides what a note plays**:

```
 SmpsChannel.events ──→ walk_channel ──→ (event, DriverState, ResolvedNote) ...
                          │
                          ├─ DriverState.apply(flag)   level, pan, transpose, detune, voice,
                          │                            PSG entry, noise form, fill, modulation
                          └─ resolve_note(...)         instrument, MOD note, path, entry,
                                                       chip pitch, detune, merged gain
```

Every pass walks this way — the conversion, the level and sustain planners, the noise
derivations, `resolve_synth_roots`, `core/merge` — so none can disagree about a note.  Only
MOD-emission state (note fill, vibrato, cursor) belongs to `ChannelWriter`.  `core/analysis.py`
keeps its own loop on purpose: it describes a song with no config.

| Module | Role |
|---|---|
| `driver_state.py` | `DriverState` (a `TrackState` plus the MOD routing), `resolve_note` → `ResolvedNote`, `walk_channel`, `enabled_channels`, `psg_range_entry` |
| `instruments.py` | The instrument catalogue: `fm_catalogue` (`FmInstrument` = list of `FmLayer`s; plain, detune variant or composite), `psg_catalogue`, `free_slots`.  The first map entry naming a slot decides what it is rendered for; both generators read it |
| `instrument_plan.py` | `prepare_instruments`: `resolve_synth_roots` then `plan_detune_variants` — the converter's first step and the audits' whole preparation; `sounding_pitches` |
| `synth_roots.py` | `resolve_synth_roots`: each rooted entry's rendering pitch, from the song |
| `detune.py` | `plan_detune_variants` → `DetunePlan`: each `smpsAlterNote` detune a sample of its own |
| `noise_derive.py` | `derive_noise_envelopes`, `derive_rate3_dividers` |
| `timeline.py` | `Timeline`: tempo segments, ticks per frame, BPM, tick → (pattern, row), seconds |
| `derive.py` | Minimal configs: `derive_config`, `complete_config` (convert.py), `load_config` (the tools), `starting_volume` |

---

## 6. The converter (`core/convert`)

`SmpsToModConverter(song, config, synth, psg_synth, generators).convert()` orchestrates; each step
is a module of its own and reports through one `Diagnostics`.

```
convert()
 ├─ _convert_once()
 │   ├─ prepare_instruments              synthesis pitches, detune variants
 │   ├─ prepare_song                     tempo-divider re-timing, short loops replayed
 │   ├─ (merged) build_merge_plan        → config.merge_plan, MergedBuild
 │   ├─ SustainPlanner.resolve           how long each instrument's sample must ring
 │   ├─ install samples                  FM generator (at LevelPlanner.fm_render_levels),
 │   │                                   sample_list files, DAC saturation, PSG generator
 │   ├─ (merged) MergedBuild.mix         pcm composites, then sample banks
 │   ├─ _convert_all_channels            LevelPlanner.levels → ChannelWriter per channel
 │   └─ ModLayout                        leading rests' C00, row-0 Fxx, mid-song tempo Fxx
 ├─ apply_pattern_breaks                 (mod/file.py)
 ├─ ModLayout.loop_point                 Bxx (+ Dxx) in the post-break layout
 ├─ trim at the loop, or song_end        D00 where a song stops
 ├─ (merged) narrow_to                   empty columns go; ≤ 4 → M.K.
 ├─ sample_names                         (samples.names: source)
 ├─ compact_samples                      slots renumbered (samples.compact_slots)
 └─ zero_idle_words                      (samples.pt_zero_bytes)
```

In a merged build `_convert_passes` may run `_convert_once` up to four times, from a copy of
the song and config, until the slots reserved for sample banks match what the banks need
(`bank_reserve_wanted`).

| Module | Role |
|---|---|
| `smps2mod.py` | `SmpsToModConverter`.  Public to the config tools: `prepare_song()`, `level_baselines()`, `pan_law_db`, `pattern_of_tick()`, `last_pattern()`, `tick_span_secs()`, `sample_secs()`; to the CLI: `convert()`, `warnings`, `infos`, `sample_sources()` |
| `generators.py` | `FmGenerator`, `PsgGenerator`, `FmDrumGenerator` protocols; `SampleGenerators` |
| `fm_drums.py` | `drum_rings`: how long each FM drum is heard (a hit to the drum track's next): its render's cap |
| `level_plan.py` | `LevelPlanner`: baked levels per instrument, FM render levels; `fm_tl_to_mod`, `psg_att_to_mod`, `modal_level` |
| `sustain_plan.py` | `SustainPlanner`: `sustain_duration: auto` per instrument, `sustain_short` warnings |
| `channel_writer.py` | `ChannelWriter`: one channel's cells — note-ons, `EDx`, cuts, release / decay slides, `3FF`, `9xx`, `Cxx`, `4xy`; `_ColumnRouter` for per-pattern columns |
| `vibrato.py` | `VibratoSpeed`, `vibrato_depth`: `smpsModSet` → `4xy` |
| `layout.py` | `ModLayout`: `leading_rests`, `tempo_commands`, `tempo_changes`, `loop_point`, `song_end` |
| `sample_names.py` | `sample_names`: what plays each slot, in 22 characters |
| `survey.py` | `SurveyContext`: the song prepared as `--merged` prepares it (merge_survey, fold_csv) |

What each step decides, and why: `docs/pipeline.md`.

---

## 7. The merged build (`core/merge`)

`convert.py --merged`: `prepare_merged_config` disables followers and packs columns; inside the
conversion `build_merge_plan` pairs each follower's notes with its primary's and allocates
composite instruments.  The plan sits on `config.merge_plan`, which `walk_channel` reads, so
every pass sees composites and spliced notes the same way.

```
 notes.py   NoteOn, channel_notes, pair_channels → PairStats       (also merge_survey, fold_csv)
 model.py   MergePlan, Composite                                    ← notes
 slots.py   fit_composites, same_shape_twins, stand_in, drop_composite, trigger_note  ← model
 pool.py    the fill pool, solo-note splicing                       ← model, notes
 plan.py    prepare_merged_config, build_merge_plan, column_sources ← all of the above
 mix.py     mix_pcm_composites                                      ← model
 banks.py   pack_banks: mixes in shared slots, chosen with 9xx      ← mix, model, slots
 build.py   MergedBuild: composite volumes, mixes, banks, report    ← banks, mix, model
```

FM composites are catalogue entries (`FmInstrument` with one layer per voice) rendered by
`ym2612.renderer.render_layers`; other composites are mixed from the generators' unquantised
renders.  Rules: `docs/pipeline.md` § The merged build.

---

## 8. The MOD file (`core/mod`)

| Module | Role |
|---|---|
| `file.py` | `ModFile` (writer: samples, patterns, positions, cell helpers, `narrow_to`, `compact_samples`, `trim_to_pattern`, `zero_idle_words`), `apply_pattern_breaks`, `row_to_bcd`; the one reader `read_mod` → `ModImage` (`play_rows`: one pass in play order), `isolate_channel` |
| `notes.py` | `PERIOD_TABLE` (C1–B3), `note_rate` / `period_rate` (a note's playback rate), `ModNote`, `MOD_NOTE_MAP` (config spellings) |
| `volume.py` | dB → MOD volume: `db_to_mod_volume`, `clamp_mod_volume`, `headroom_db` |
| `limits.py` | `MAX_MOD_SAMPLE_BYTES`, `sample_limit_bytes`, `max_sustain_secs` |
| `timing.py` | `timed_pass`, `edx_delay`: when each row plays — the MOD clock of the audits |
| `sample_audit.py` | A written MOD's samples against the notes that play them (`tools/mod_audit.py`, the report's Samples table) |

The cell layout lives only in `file.py` (`cell_index`, `effect_at`, `effect_slot_free`, …).

### Binary layout

```
Offset  Size   Content
0       20     song name (19 characters + NUL)
20      930    31 sample headers × 30 bytes: name 22, length (words) 2, finetune 1,
               volume 1, loop start (words) 2, loop length (words) 2
950     1      song length (positions used)
951     1      restart byte (127)
952     128    position list (pattern numbers)
1080    4      format tag: "M.K." (4 ch), "8CHN", "10CH", "12CH", "14CH", "16CH"
1084    …      patterns: 64 rows × channels × 4 bytes
…       …      sample data, signed 8-bit, in slot order
```

A cell:

```
byte 0  instrument high nibble | period bits 11–8
byte 1  period bits 7–0
byte 2  instrument low nibble  | effect command
byte 3  effect parameter
```

Effects: `docs/mod_effects.txt`.  Limits the conversion works within: `docs/pipeline.md` § MOD
limits.

---

## 9. Chips, audio and sample rendering

| Module | Role |
|---|---|
| `core/chips/fm.py` | YM2612 facts: clock, sample rate, carriers by algorithm, `fm_frequency_hz`, the level law (`TL_STEP_DB` 0.75, pan law, `fm_level_db`) |
| `core/chips/psg.py` | SN76489 facts: clock, `psg_frequency_hz`, `PSG_STEP_DB` 2.0, `psg_level_db` |
| `core/audio/gain.py` | `db_to_gain`, `gain_to_db`, `power_to_db` |
| `core/audio/pcm.py` | Mono / int8 helpers: dithered quantiser (`to_int8`, `full_scale_int8`), `dc_block`, `high_shelf`, `saturate`, `limit_peaks` |
| `core/audio/resample.py` | Polyphase windowed-sinc resampler (FM, PSG, SFX) |
| `core/audio/loops.py` | Sustain loops: `find_sustain_loop`, `apply_loop`, `release_rate_db_s`, `heard_padding` |
| `core/audio/pitch.py` | Hz ↔ MIDI and semitones from C0 (`semitone_to_hz`), note names, cents |
| `core/render_cache.py` | `RenderCache`: chip renders on disk by a hash of their inputs and of the code |
| `core/files.py` | `write_atomic`, `write_shared`: files parallel conversions share (the cache, a minimal config's DAC samples), never read half-written |
| `core/cbuild.py` | `CLibrary`: compile a C emulator with gcc / MSVC, rebuilt when a source is newer |
| `ym2612/` | `build.py` (Nuked-OPN2 → DLL), `wrapper.py` (`OPN2`), `voice.py` (`program_voice`), `renderer.py` (`render_note`, `render_layers`), `sample_generator.py` (`generate_fm_samples`), `validate.py` |
| `sn76489/` | `build.py`, `wrapper.py` (`SN76489`), `renderer.py`, `sample_generator.py` (`generate_psg_samples`), `validate.py` |
| `sfx/` | The offline SFX driver behind `sonic2wav.py`: `docs/sfx_rendering.md` |

Rendering in detail: `docs/fm_synthesis.md`, `docs/psg_synthesis.md`.

---

## 10. Verification (`core/audit`, `tools/`, `tests/`)

| Where | What |
|---|---|
| `core/audit/pitch.py` | Symbolic pitch audit: each MOD note's pitch against the chip's frequency registers (`tools/vgm_pitch_audit.py`, and the verdict inside `vgm_compare`) |
| `core/audit/render.py`, `signal.py`, `levels.py`, `onsets.py` | `vgm_compare`'s per-channel renders (VGMPlay, ffmpeg + libopenmpt), measures, per-instrument levels, onset matching |
| `core/audit/rip_diff.py` | `compare_with_rip(song, frames, aspects, ChannelChoice, lift)` → `RipDiff`: a song (`SongSource`: asm, or ROM + sound, as shipped) against its rip lifted at the song's tempo (`LiftTempo`) - `tools/vgm_lift.py` |
| `core/audit/rips.py` | `RipShelf`: a config's rip and a rip's config, by number or the `rips.yaml` beside the configs (`vgm_lift`, `measure_volumes`) |
| `tools/` | `vgm_analyze`, `vgm_compare`, `vgm_pitch_audit`, `vgm_lift`, `measure_volumes`, `rom_import`, `mod_compare`, `mod_lint`, `mod_audit`, `mod_render_diff`, `merge_survey`, `fold_csv`, `config_to_chip_space`, `make_credits_config`, `release` |
| `tests/regression.py` | Every config (and its merged build, variants, ROM cases) converted and compared with a baseline MOD, cells and samples |
| `tests/tool_regression.py` | The VGM tools' output, byte for byte |
| `tests/test_*.py` | Unit tests (`python -m pytest tests -q`) |

How to run them: `CLAUDE.md` § Regression Testing; the VGZ workflow: `docs/pipeline.md`
§ Verifying against a VGZ.

---

## 11. Reporting and CLIs

The converter reports through `Diagnostics` (`core/diagnostics.py`): `diag.warn(WarningKind.X,
...)` / `diag.info(InfoKind.Y, ...)`, de-duplicated, exposed as `converter.warnings` /
`converter.infos` (dicts whose `type` is the kind).  `core/ui/report.py` prints the report —
header, Checks, Channels (merged: Columns), Samples (read back from the written file), Merge,
and with `--verbose` Details.  Every `WarningKind` has an entry in `_WARNINGS` returning
`(check, headline, fix)`; `tests/test_diagnostics_units.py` checks none is missing.  A new
warning is one enum member, one function, one entry.

| CLI | Does | Reference |
|---|---|---|
| `convert.py` | config → MOD (`from_yaml` → `read_song` → `complete_config` if minimal → `--merged` → `derive_bpm` → `load_settings` → `convert()` → write → `print_report`) | `docs/yaml_config.md` § Running a config |
| `analyze.py` | A song with no config (Rich tables, `--config` coverage diff, `--write` YAML skeleton) | `CLAUDE.md` § Quick Usage |
| `sonic2wav.py` | SFX → WAV / 8-bit raw | `docs/sfx_rendering.md` |

`core/ui/cli.py` holds the shared chrome (`cli_console`, `branding`, `add_variant_argument`);
`core/ui/pitch_audit.py` and `core/ui/song_diff.py` print the pitch audit and song diffs.
