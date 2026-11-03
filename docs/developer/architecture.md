# Architecture

For someone about to change the code: how data flows from a song to a MOD, how `core/` is layered, what
each package and module holds, the order `convert()` runs its steps in, the MOD's bytes, and how changes are
tested.  What the conversion decides (and why) lives in [`../conversion/`](../conversion/overview.md); every
config key in [`../guide/song_configs.md`](../guide/song_configs.md).

On this page:

1. [Data flow](#data-flow)
2. [Layers](#layers)
3. [Packages and modules](#packages-and-modules)
4. [The converter: `convert()` step order](#the-converter-convert-step-order)
5. [MOD binary layout](#mod-binary-layout)
6. [Reporting and CLIs](#reporting-and-clis)
7. [Testing](#testing)
8. [Developer setup and checks](#developer-setup-and-checks)
9. [Where do I change X](#where-do-i-change-x)

---

## Data flow

### Reading: three sources, one IR

Every input becomes one intermediate representation (IR), `SmpsSong`: channels of note and effect events,
the song's voices, and its driver's [`PlaybackRules`](../glossary.md#playbackrules) (tables, envelopes,
timing).  `core.source.read_song(path)` picks the front end by file suffix.

```
  .asm                     ROM + sound ID                 .vgm / .vgz rip
    │                           │                               │
 SmpsParser                read_rom_code                  frame_log → lift_song
 (smps/parser.py)          (drivers/read.py)              (vgm/frames.py, vgm/lift/)
    │                           │                               │
    └──────── SmpsCode ─────────┘                               │
                 │                                              │
           song_from_code  (smps/code.py: the walk through      │
                 │          the track code, shared)             │
                 └──────────────────┬───────────────────────────┘
                                    ▼
                                SmpsSong
```

### Converting: one MOD channel per song channel

```
 SmpsSong ─┐
           ├─ prepare_conversion ─→ SmpsToModConverter.convert() ─→ ModFile ─→ .mod
 config ───┘   (convert/prepare.py)       │
                                          ├─ plan:   walk_channel → instrument, MOD note, level
                                          ├─ render: instrument catalogue → FM / PSG generators → samples
                                          └─ write:  ChannelWriter per channel → cells; ModLayout → tempo, loop, end
```

### Rendering a sample

```
 config maps ─→ instrument catalogue ─→ generate_fm_samples  (synth/) ─→ OPN2    (chips/ym2612/)  ─┐
 (voice_map,    (plan/instruments.py)   generate_psg_samples (synth/) ─→ SN76489 (chips/sn76489/) ─┴→ {slot: (pcm, rate)}
  psg maps)                                                                                          + loop, release rate
```

The [merged build](../guide/amiga_builds.md) adds a merge plan between planning and writing: it
[folds](../glossary.md#fold) channels together and adds [composite](../glossary.md#composite) instruments to
the same catalogue.

### One state machine decides what a note plays

```
 SmpsChannel.events ──→ walk_channel ──→ (event, DriverState, ResolvedNote) ...
                          │
                          ├─ DriverState.apply(flag)   level, pan, transpose, detune, voice,
                          │                            PSG entry, noise form, fill, modulation
                          └─ resolve_note(...)         instrument, MOD note, path, entry,
                                                       chip pitch, detune, merged gain
```

Every pass walks this way (`core/plan/driver_state.py`): the channel writers, the level and sustain
planners, the noise derivations, `resolve_synth_roots`, `core/merge`.  So no two passes can disagree about
a note.  Only MOD-emission state (note fill, vibrato, cursor) belongs to `ChannelWriter`.
`core/analysis/song.py` keeps its own loop on purpose: it describes a song with no config.

> **Pitfall:** never re-implement the walk.  A new pass that needs "what does this note play" calls
> `walk_channel`.

---

## Layers

`pyproject.toml [tool.importlinter]` enforces this; `tests/test_layers.py` runs it.  A package imports only
the packages below it.  The list is exhaustive: a new package under `core/` fails until it is given a layer.

```
 convert.py  analyze.py  sonic2wav.py      CLIs (thin)
 tools/                                    utilities: vgm/ mod/ config/ rom/ chips/ dev/
 sfx/                                      the offline SFX driver
 ───────────────────────────────────────── core/ imports none of the above
 core/  ui
        convert | audit | analysis
        merge | synth
        plan
        config
        source
        vgm | drivers
        rom
        smps | mod
        chips | audio | render_cache
        files
                                           outside the stack: diagnostics (any layer reports),
                                           version (read by the CLIs)
```

Names on one line (`a | b`) are independent of each other.  The other contracts:

| Contract | Rule |
|---|---|
| drivers through `core.source` alone | `ui`, `convert`, `audit`, `analysis`, `merge`, `synth`, `plan`, `config` never import `core.drivers` |
| driver folders independent | no driver imports another; shared code lives in its family folder (`smps68k/`, `smpsz80/`) |
| families independent | `smps68k`, `smpsz80`, `mucom`, `timetrax` import nothing of each other |
| registry loads drivers | only `core.drivers.registry` imports a family package (lazily, on first use) |
| Sonic 1's tables | `core.drivers.reference` is imported only by the SMPS families and `sfx` |
| no SFX in core | `core` never imports `sfx` |
| acyclic siblings | no import cycle between a package's modules |

Conventions:

- A package's `__init__.py` exports what other packages import (`from ..smps import SmpsSong`); its own
  modules import each other directly, so a module can move inside its package unseen.
- `smps` imports only `chips` and `audio`; `mod` only `audio`; `chips`, `audio`, `files` nothing of `core`.
- The chips are kept as MAME keeps devices: `core/chips/ym2612/` and `core/chips/sn76489/` are emulators
  alone (registers in, samples out), beside their facts (`fm.py`, `psg.py`).  Their C cores are vendored
  in `3rdparty/`, compiled into `build/` on first use; with no compiler, `prebuilt/`'s Windows DLLs load.
  Anything may drive a device: `core/synth` for the converter, `sfx/` for the SFX driver.
- A song carries its driver's `PlaybackRules`: code reads `song.rules`, never a driver's table directly.
  Nothing below `core/drivers` falls back to Sonic 1's tables.

---

## Packages and modules

One line each.  Every module listed exists under `core/`; `__init__.py` files omitted unless they hold code
worth naming.

### `core/smps` — the IR and the shared walk

| Module | Holds |
|---|---|
| `song.py` | `SmpsSong` (header, channels, voices, `rules`, `fm_drums`, `dropped`), `SmpsChannel`, `SmpsEvent`, `SmpsNote`, `SmpsVoice` (`registers(tl_offset)`), header classes |
| `effects.py` | One frozen class per `CoordFlag` meaning, operands named (`SetVoice(index)`, `ModSet(wait, speed, delta, steps)`); `PlayedEffect` (kept in events) vs `DriverEffect` (resolved by the walk) |
| `rules.py` | `PlaybackRules` and each kind of track's `TrackRules` (volume, pitch, modulation, ties, key-off) |
| `parser.py` | `SmpsParser`: SMPS2ASM text → `SmpsCode` → `SmpsSong` ([`../drivers/smps_asm.md`](../drivers/smps_asm.md)) |
| `code.py` | `SmpsCode`, `Op`, `SongCode`; `song_from_code`: each channel walked through the ops (durations, `smpsNoAttack`, loops, calls, fall-through).  Asm and ROM share it |
| `asm_writer.py` | `write_asm`: a `SongCode` back to SMPS2ASM text |
| `driver_tables.py` | What every SMPS driver reads tables by: note indices, `chip_pitch`, `PsgEnvelope`, `SMPS_OP_TO_REG_OFFSET`, pan values.  No driver's tables |
| `driver_track.py` | `DriverTrack`: what a track's driver does beyond Sonic 1's reading (volume steps, gates, drum samples, noise dividers) |
| `track.py` | `TrackState`: one track's driver state as its flags leave it |
| `names.py` | Note labels in both spellings, `parse_smps_note`, `parse_synth_note`, DAC and flag names |
| `song_prep.py` | `prepare_song`: a copy as played (`smpsSetTempoDiv` re-timing, short loops replayed) |
| `adsr.py` | `Adsr`: a software ADSR envelope (Streets of Rage 2's PSG) |
| `pitch_envelope.py` | A driver's pitch envelope, one step a frame (Space Harrier II's, OutRun's) |
| `slide.py` | `PitchSlide`: a track's frequency word frame by frame under a glide / portamento (Time Trax's) |
| `volume_envelope.py` | `EnvelopeTrack`: a track's volume frame by frame under a volume envelope (Time Trax's) |
| `lfo.py` | `apply_lfo`: voice copies under the hardware LFO (Streets of Rage's `$FC`) |
| `voice_patch.py` | `VoicePatcher`: patched voice copies (register writes, FM3 special-mode offsets, LFO) |
| `percussion.py` | `FmDrum`, `FmFrame`: an FM drum as the chip plays it, frame by frame |
| `run_out.py` | `apply_run_out`: a driver's key-on run-out (Golden Axe: 256 frames) |
| `clock.py` | `UpdateClock` (`VInt`, `TimerA`), `update_hz`: the one source of a frame's length |
| `tempo.py` | `tempo_schedule`: the frame each tick is read on |
| `playback.py` | `played_song`: each note as the driver plays it (`PlayedNote`) |
| `compare.py` | `compare_songs`, `align_songs`: two songs note by note per `Aspect` |

### `core/rom` — the ROM framework (names no driver)

| Module | Holds |
|---|---|
| `image.py` | A Mega Drive ROM image: header, big-endian reads by address |
| `variant.py` | `SmpsVariant`: what one driver differs by (memory, locate, flags, grammar, layouts, DAC) |
| `memory.py` | `SoundMemory`: how a driver reads pointers |
| `flags.py` | A flag table's words; each kind of track's flags as one table |
| `header.py` | `HeaderLayout`, `EntryLayout`: a song header's geometry |
| `tracks.py` | Follows track code → `SmpsCode` |
| `grammar/` | Track grammars: `smps.py` (SMPS), `mucom.py` (MUCOM language, a `MucomDialect` per driver) |
| `voices.py` | `VoiceLayout`: a voice's byte order |
| `envelopes.py` | PSG envelopes read from a ROM |
| `fixes.py` | `RomFix`, `apply_fixes`: data-bug fixes |
| `pattern.py` | `code_pattern`: a run of code to find, written as a listing |
| `z80.py`, `kosinski.py` | Z80 RAM as the 68k loads it (copy loops, Kosinski decompression) |
| `pcm/` | DAC sample tables as a Z80 player stores them: `smps.py` (Sonic 1 lineage), `sor.py` (Streets of Rage) |

### `core/drivers` — one folder per driver, by family

| Module | Holds |
|---|---|
| `registry.py` | `load_driver`: each driver loaded on first use |
| `games.py` | Each known ROM by SHA-1: game, driver, `data_fixes` |
| `detect.py` | `detect_variant` |
| `read.py` | `locate_sounds`, `read_rom_code`, `read_rom_song`, `dac_samples` |
| `names.py` | Each driver's name (a config's `driver:`) |
| `reference.py` | Sonic 1's tables and `SONIC1_RULES` |
| `smps68k/` | Shared `common.py`, `memory.py`, `locate.py`, `dac.py`; drivers `sonic1/`, `type1a/` (Moonwalker), `streets_of_rage/` |
| `smpsz80/` | Shared `memory.py` (bank window), `program.py`, `pitch_envelopes.py`; drivers `type0fm/` (Golden Axe), `sh2/` (Space Harrier II), `outrun/` |
| `mucom/md/` | Mucom-MD 0.85 (Streets of Rage 2) |
| `timetrax/` | Tim Follin's Z80 driver (Time Trax): `walk.py`, `glide.py`, `envelope.py`, `volume.py` and the readers |

Each driver folder holds some of `variant.py`, `locate.py`, `memory.py`, `header.py`, `layout.py`,
`grammar.py`, `voices.py`, `envelopes.py`, `drums.py`, `dac.py`.  Per-driver facts:
[`../drivers/`](../drivers/README.md).

### `core/vgm` — rips (VGM register logs)

| Module | Holds |
|---|---|
| `reader.py` | `read_vgm` → `VgmLog` |
| `chipstate.py` | `ChipState.replay`: registers write by write |
| `writes.py` | `decoded_writes`: each write's channel and meaning (`vgm_analyze --writes`) |
| `bursts.py` | `write_bursts`: writes grouped into bursts |
| `spans.py` | Bursts gathered into the driver's updates |
| `clock.py` | Where a log's frames fall (V-int period, or Timer A resets) |
| `frames.py` | `frame_log`: every channel's state per frame |
| `updates.py` | `driver_updates`: each update's timing, a lost V-int flagged (`vgm_analyze --updates`) |
| `realign.py` | `realigned`: the log with lost or gained V-ints undone |
| `notes.py` | `note_starts`, `pitch_segments` |
| `cache.py` | `load_frames`, cached |
| `lift/` | `lift_song`: a frame log → `SmpsSong` ([lift](../glossary.md#lift); `tracks.py`, `tempo.py`, `song.py`) |

### `core/source`, `core/config`

| Module | Holds |
|---|---|
| `source/load.py` | `read_song(path, ...)`: picks asm / ROM / VGM by suffix; `read_dac` |
| `config/loader.py` | `load_yaml` (refuses a duplicate key), `apply_variant` |
| `config/entries.py` | `ChannelConfig`, `InstrumentRange`, `PsgInstrumentEntry`, `DacSampleConfig`, `MergeGroup`, one parser per section |
| `config/song.py` | `ConversionConfig`: `from_yaml(path, variant)` (unknown keys are errors), `read_song()`, `is_minimal` |
| `config/settings.py` | `SynthesisSettings`, `PsgSynthesisSettings`; `load_settings`, `with_song_overrides` |
| `config/bpm.py` | `derive_bpm`, `exact_bpm`, `bpm_report` |

Settings are read everywhere; a code constant is only the fallback when a key is absent
([`../guide/settings.md`](../guide/settings.md)).

### `core/plan` — the song read through its config

| Module | Holds |
|---|---|
| `conversion.py` | `ConversionPlan`: one conversion's decisions (render pitches, detune variants, merge plan, `SampleTable`), kept off the config |
| `driver_state.py` | `DriverState`, `resolve_note` → `ResolvedNote`, `walk_channel`, `enabled_channels` |
| `instruments.py` | The instrument catalogue: `fm_catalogue` (`FmInstrument` = `FmLayer`s), `psg_catalogue`, `free_slots` |
| `instrument_plan.py` | `prepare_instruments`: render pitches and detune variants, the converter's first step |
| `synth_roots.py` | `resolve_synth_roots`: each entry's rendering pitch ([synth_root](../glossary.md#synth_root)) |
| `detune.py` | `plan_detune_variants` → `DetunePlan` ([detune variant](../glossary.md#detune-variant)) |
| `glides.py` | `channel_reads`: each read's word and TLs frame by frame; `reaches` |
| `baked.py` | `plan_baked` → `BakedPlan`: a sweep or moving level rendered into a sample |
| `noise_derive.py` | `derive_noise_envelopes`, `derive_rate3_dividers` |
| `timeline.py` | `Timeline`: tempo segments, ticks per frame, BPM, tick → (pattern, row), seconds |
| `meter.py` | `find_meter`: step, row, phrase, downbeat (`row_grid: bar`) |
| `derive.py` | [Minimal configs](../glossary.md#minimal-config): `derive_config`, `complete_config`, `load_config` |
| `slot_fit.py` | A minimal config fitted to 31 slots |

### `core/convert` — the converter

| Module | Holds |
|---|---|
| `smps2mod.py` | `SmpsToModConverter(song, config, synth, psg_synth)`: `convert()` and the passes |
| `prepare.py` | `prepare_conversion` → `PreparedConversion`: a config set up (minimal one completed, merged columns, `auto_bpm`, settings).  `convert.py` and the audit tools start here |
| `survey.py` | `SurveyContext`: a song prepared as `--merged` prepares it (`merge_survey`, `fold_csv`) |
| `samples.py` | `SampleInstaller`: one pass's samples rendered or loaded, DAC drums saturated |
| `sustain_plan.py` | `SustainPlanner`: `sustain_duration: auto` per instrument |
| `level_plan.py` | `LevelPlanner`: baked levels, FM render levels; `fm_tl_to_mod`, `psg_att_to_mod` |
| `normalise.py` | `normalise_volumes`: volumes raised until the loudest note plays at 64 |
| `fm_drums.py` | `drum_rings`: how long each FM drum is heard |
| `vibrato.py` | `smpsModSet` → `4xy`; `curve_slides`: a pitch curve as row slides |
| `layout.py` | `ModLayout`: leading rests, tempo `Fxx`, loop `Bxx`, song end `D00` |
| `sample_names.py` | `sample_names`: what plays each slot, in 22 characters |
| `channel_writer/` | One channel into cells: `writer.py` (`ChannelWriter`), `cells.py` (row addressing, cuts, `EDx`, `E1x`/`E2x`), `levels.py`, `modulation.py`, `fades.py` (release slides), `pitch_curve.py`, `level_curve.py`, `router.py` (merged columns), `note.py`, `note_warnings.py`, `context.py` |

### `core/merge` — the merged (Amiga) build

```
 notes.py   NoteOn, channel_notes, pair_channels → PairStats       (also merge_survey, fold_csv)
 model.py   MergePlan, Composite                                    ← notes
 slots.py   fit_composites, stand-ins, dropped composites           ← model
 pool.py    the fill pool, solo-note splicing                       ← model, notes
 plan.py    prepare_merged_config, build_merge_plan                 ← all of the above
 mix.py     mix_pcm_composites                                      ← model
 banks.py   pack_banks: mixes sharing a slot, chosen with 9xx       ← mix, model, slots
 build.py   MergedBuild: composite volumes, mixes, banks, report    ← banks, mix, model
```

`prepare_merged_config` disables [followers](../glossary.md#follower) and packs columns; `build_merge_plan`
pairs each follower's notes with its [primary](../glossary.md#primary)'s and allocates composites.  The plan
sits on the `ConversionPlan`, so `walk_channel` sees composites and spliced notes.  FM composites render
through `core.synth.fm_render.render_layers`; others are mixed from unquantised renders.  Rules:
[`../guide/amiga_builds.md`](../guide/amiga_builds.md).

### `core/mod` — the MOD file

| Module | Holds |
|---|---|
| `cell.py` | `Cell`: the one place that knows a cell's 4 bytes; `ROWS_PER_PATTERN` |
| `file.py` | `ModFile` (writer; cells addressed by (pattern, row, channel); `narrow_to`, `compact_samples`, `trim_tails`, `zero_idle_words`), `ModPattern`, `read_sample` |
| `reader.py` | `read_mod` → `ModImage` (`play_rows`, `play_order`) |
| `breaks.py` | `apply_pattern_breaks`, `shift_for_breaks`, `row_to_bcd` |
| `notes.py` | `PERIOD_TABLE` (C-1..B-3), `note_rate`, `ModNote`, `MOD_NOTE_MAP` |
| `volume.py` | dB → MOD volume |
| `limits.py` | `MAX_MOD_SAMPLE_BYTES`, `max_sustain_secs` |
| `reach.py` | `heard_bytes`: the MOD played tick by tick as ProTracker does: how far each one-shot is heard |
| `timing.py` | `timed_pass`, `edx_delay`: when each row plays |
| `sample_audit.py` | Samples against the notes that play them (`mod_audit`) |

### `core/chips`, `core/synth`, `core/audio` — sound

| Module | Holds |
|---|---|
| `chips/fm.py` | YM2612 facts: clock, carriers by algorithm, `fm_frequency_hz`, level law (`TL_STEP_DB` 0.75) |
| `chips/psg.py` | SN76489 facts: clock, `psg_frequency_hz`, `PSG_STEP_DB` 2.0 |
| `chips/ym2612/` | The device: `wrapper.py` (`OPN2`), `build.py` (Nuked-OPN2 → `build/`) |
| `chips/sn76489/` | The device: `wrapper.py` (`SN76489`), `build.py` (VGMPlay's core → `build/`) |
| `chips/cbuild.py` | `CLibrary`: compile a C core with gcc / MSVC, rebuilt when a source is newer; else `prebuilt/` |
| `synth/fm_voice.py` | `program_voice`: an `SmpsVoice` → YM2612 registers |
| `synth/fm_render.py` | `render_note`, `render_layers` (composites), `render_frames` (FM drums) |
| `synth/fm_samples.py` | `generate_fm_samples`, `generate_fm_drums`: the catalogue's FM instruments, threaded, cached |
| `synth/psg_render.py`, `psg_samples.py` | PSG tone and noise renders; `generate_psg_samples` |
| `audio/gain.py` | `db_to_gain`, `gain_to_db` |
| `audio/pcm.py` | Dithered int8 quantiser, `dc_block`, `saturate`, `limit_peaks` |
| `audio/resample.py` | Polyphase windowed-sinc resampler |
| `audio/loops.py` | Sustain loops: `find_sustain_loop`, `apply_loop`, `release_rate_db_s` |
| `audio/pitch.py` | Hz ↔ semitones, note names, cents |
| `render_cache.py` | `RenderCache`: chip renders on disk, keyed on inputs and code |
| `files.py` | `write_atomic`, `write_shared`: files parallel conversions share |

Detail: [`../synthesis/fm.md`](../synthesis/fm.md), [`../synthesis/psg.md`](../synthesis/psg.md).

### `core/audit`, `core/analysis`, `core/ui`

| Module | Holds |
|---|---|
| `audit/pitch.py` | Symbolic pitch audit (`vgm_pitch_audit`) |
| `audit/render.py`, `signal.py`, `levels.py`, `onsets.py` | `vgm_compare`: renders, measures, per-instrument levels, onsets |
| `audit/rip_diff.py` | `compare_with_rip`: a song against its lifted rip (`vgm_lift`) |
| `audit/frame_check.py` | `check_frames`: a song against its rip's frame log, no lift (`vgm_frames`) |
| `audit/glitch_scan.py` | `scan_glitches`: a rip's lost / gained V-ints (`vgm_frames --glitches`) |
| `audit/dac_stall.py` | `dac_stall` (`vgm_analyze --dac-stall`) |
| `audit/rips.py` | `RipShelf`: a config's rip by number or `rips.yaml`; `RipFaults` |
| `analysis/song.py`, `skeleton.py` | `analyze.py`'s model of a song with no config, and its YAML skeleton |
| `ui/report.py` | The conversion report |
| `ui/cli.py` | Shared CLI chrome (`cli_console`, `add_variant_argument`) |
| `ui/pitch_audit.py`, `song_diff.py` | Printing the pitch audit and song diffs |
| `diagnostics.py` | `Diagnostics`: warnings and infos (outside the layers) |
| `version.py` | The version, read from `pyproject.toml` |

`sfx/` (outside `core/`) is the offline SFX driver behind `sonic2wav.py`:
[`../guide/sound_effects.md`](../guide/sound_effects.md).

---

## The converter: `convert()` step order

`SmpsToModConverter.convert()` (`core/convert/smps2mod.py`).  The order is fixed: the loop's `Bxx` needs the
post-break layout.

```
convert()
 ├─ _convert_passes()                    _convert_once(), repeated for a merged build's bank reserve
 │   └─ _convert_once()
 │       ├─ plan_instruments             ConversionPlan: render pitches, detune variants
 │       ├─ prepare_song                 tempo-divider re-timing, short loops replayed
 │       ├─ (merged) build_merge_plan    → ConversionPlan.with_merge, MergedBuild
 │       ├─ SustainPlanner.resolve       how long each sample must ring
 │       ├─ SampleInstaller.install      FM (at LevelPlanner's render levels), sample_list files,
 │       │                               DAC saturation, PSG
 │       ├─ (merged) MergedBuild.mix     PCM composites, then sample banks
 │       ├─ _convert_all_channels        ChannelWriter per channel (normalise_volumes: a scratch
 │       │                               write first, then the volumes raised)
 │       └─ ModLayout                    leading rests' C00, row-0 Fxx, mid-song tempo Fxx
 ├─ apply_pattern_breaks                 (mod/breaks.py)
 ├─ ModLayout.loop_point                 Bxx (+ Dxx)
 ├─ trim_to_pattern                      at the loop, or at song_end's D00 for a song that stops
 ├─ (merged) narrow_to                   empty columns dropped; 4 left → M.K.
 ├─ sample_names                         samples.names: source
 ├─ compact_samples                      slots renumbered (samples.compact_slots)
 ├─ trim_tails                           one-shots cut to what is heard (samples.trim_tails, mod/reach.py)
 └─ zero_idle_words                      samples.pt_zero_bytes
```

In a merged build `_convert_passes` reruns `_convert_once` (at most 4 builds, `_MAX_BANK_BUILDS`) from the
song as given until the slots held back for sample [banks](../glossary.md#bank) match what the banks need
(`bank_reserve_wanted`).  What each step decides: [`../conversion/`](../conversion/overview.md).

---

## MOD binary layout

```
Offset  Size   Content
0       20     song name, ASCII, NUL padded (20 characters fill it, no terminator)
20      930    31 sample headers × 30 bytes: name 22, length (words) 2, finetune 1,
               volume 1, loop start (words) 2, loop length (words) 2
950     1      song length (positions used)
951     1      restart byte (127)
952     128    position list (pattern numbers)
1080    4      format tag: "M.K." (4 ch), "8CHN", "10CH", "12CH", "14CH", "16CH"
1084    …      patterns: 64 rows × channels × 4 bytes
…       …      sample data, signed 8-bit, in slot order, exactly the header's length
```

A cell (`core/mod/cell.py`, the only code that knows it):

```
byte 0  instrument high nibble | period bits 11–8
byte 1  period bits 7–0
byte 2  instrument low nibble  | effect command
byte 3  effect parameter
```

Effects: [`../reference/mod_effects.md`](../reference/mod_effects.md).  Limits the conversion works within:
[`../conversion/overview.md`](../conversion/overview.md).

---

## Reporting and CLIs

The converter reports through `Diagnostics` (`core/diagnostics.py`): `diag.warn(WarningKind.X, ...)` /
`diag.info(InfoKind.Y, ...)`, de-duplicated, exposed as `converter.warnings` / `converter.infos`.
`core/ui/report.py` prints them.  Every `WarningKind` has an entry in `_WARNINGS` returning
`(check, headline, fix)`; `tests/core/test_diagnostics.py` checks none is missing.

> **Note:** a new warning is one enum member, one function, one `_WARNINGS` entry.

| CLI | Does |
|---|---|
| `convert.py` | config → MOD: `ConversionConfig.from_yaml` → `prepare_conversion` → `SmpsToModConverter.convert()` → write → report.  A folder converts every config in parallel |
| `analyze.py` | A song with no config: tables, `--config` coverage diff, YAML skeleton |
| `sonic2wav.py` | SFX → WAV / 8-bit raw |

---

## Testing

Full workflow: `CLAUDE.md` § Regression Testing.  In short:

| Suite | What it checks |
|---|---|
| `python -m pytest tests -q` | Unit tests.  `tests/core/<package>/test_<module>.py` tests `core/<package>/<module>.py`; `tests/tools/` the tools.  Folders are not packages (`--import-mode=importlib`) |
| `tests/regression.py` | Every case of `tests/cases.yaml` converted and compared with its baseline MOD (`tests/baselines/`): cells, sample table and data, byte for byte |
| `tests/tool_regression.py` | The tools' output byte for byte: `vgm_analyze`, `vgm_pitch_audit`, `vgm_lift`, `vgm_frames`, `song_dump` (every game's songs as read); `--with-renders` adds `vgm_compare` |
| `tests/test_layers.py` | The import contracts |

**Case selection.**  `--generate-baselines` records, under coverage.py, the files and functions each case
runs (`tests/baselines/coverage.yaml`, `functions.yaml`).  A plain run diffs the working tree against each
case's baseline commit and runs only the cases the change can move (`tests/selection.py`,
`tests/functions.py`): a changed function moves the cases that ran it; code outside a function every case
that loaded its file.  `--all` runs everything; `--only NAME ...` picks cases or groups.

Cases convert with `tests/settings.yaml`, never `configs/settings.yaml`, so tuning by ear moves no baseline.
A case whose ROM is missing from `input/roms/` is skipped (`tests/roms.py`).

```bash
python tests/regression.py --generate-baselines   # before a change, on a commit
python tests/regression.py                        # after: the cases it can move
python tests/regression.py --only title_screen    # one case; outputs in output/regression/
```

> **Pitfall:** a FAIL is not an acceptance.  Read each changed cell (`tools/mod/mod_compare.py`,
> `tools/mod/mod_lint.py`) before regenerating that case's baseline.

---

## Developer setup and checks

```bash
pip install -e .[dev]   # numpy, pyyaml, rich; dev: ruff, pyright, vulture, pytest, coverage, import-linter,
                        # capstone + z80dis (tools/rom/disasm.py)
git config core.hooksPath tools/dev/hooks   # once per clone
```

| Command | Runs |
|---|---|
| `python tools/dev/check.py` | ruff, pyright, vulture, import layers, text encoding, unit tests, side by side (~40 s) |
| `python tools/dev/check.py --fast` | the same without the unit tests (~7 s); the pre-commit hook |
| `python tools/dev/check.py --regress` | all of it, then both regression suites |
| `python tools/dev/text_encoding.py` | every text file UTF-8, none garbled |
| `python tools/dev/release.py patch` | bump the version in `pyproject.toml`, commit, tag |

Git hooks (`tools/dev/hooks/`): `pre-commit` runs `text_encoding.py` always and `check.py --fast` when a
`.py` / `.toml` file is staged; `commit-msg` refuses a `Co-Authored-By` trailer.  `ruff_after_edit.py` is a
Claude Code hook (`.claude/settings.json`).  Dead-code false positives go in `vulture_whitelist.py`.

After changing a C source in `3rdparty/` (or `ym3438_batch.c`), copy the rebuilt `build/*.dll` into
`prebuilt/` and commit them together.

---

## Where do I change X

| Task | File |
|---|---|
| How a coordination flag changes driver state | `core/plan/driver_state.py` (`DriverState.apply`) |
| Which instrument / MOD note a note resolves to | `core/plan/driver_state.py` (`resolve_note`) |
| A new effect meaning a driver can hold | `core/smps/effects.py`, then the walk (`core/smps/driver_track.py`) |
| Which MOD effect a note or flag writes | `core/convert/channel_writer/` (`writer.py`, `modulation.py`, `fades.py`, `cells.py`) |
| Vibrato depth / speed arithmetic | `core/convert/vibrato.py` |
| Tempo, loop `Bxx`, song end `D00` | `core/convert/layout.py` |
| Pattern breaks | `core/mod/breaks.py` |
| Levels and volumes | `core/convert/level_plan.py`, `channel_writer/levels.py`, `core/mod/volume.py` |
| Sample length / sustain | `core/convert/sustain_plan.py`, `core/audio/loops.py` |
| FM rendering | `core/synth/fm_render.py`, `fm_voice.py`; chip facts `core/chips/fm.py` |
| PSG rendering | `core/synth/psg_render.py`; chip facts `core/chips/psg.py` |
| Rendering pitch (`synth_root`) | `core/plan/synth_roots.py` |
| Detune variants | `core/plan/detune.py` |
| A new config key | `core/config/entries.py` or `song.py`; document in `docs/guide/song_configs.md` |
| A new setting | `core/config/settings.py`, `configs/settings.yaml` **and** `tests/settings.yaml` |
| Minimal-config derivation | `core/plan/derive.py`, `slot_fit.py` |
| The merged build's folds / composites / banks | `core/merge/` (`plan.py`, `slots.py`, `pool.py`, `banks.py`) |
| Parsing SMPS asm | `core/smps/parser.py`; walk shared with ROMs `core/smps/code.py` |
| A new driver | a folder in `core/drivers/<family>/`, a line in `pyproject.toml`'s independence contract, `core/drivers/games.py`, `tests/roms.py` ([`../drivers/README.md`](../drivers/README.md)) |
| A new known ROM | `core/drivers/games.py`, `tests/roms.py` |
| Reading a VGM rip | `core/vgm/` |
| MOD bytes | `core/mod/cell.py`, `core/mod/file.py` |
| A new warning | `core/diagnostics.py` (`WarningKind`), `core/ui/report.py` (`_WARNINGS`) |
| A new regression case | `tests/cases.yaml`, then `--generate-baselines --only <name>` |
| A new package under `core/` | `pyproject.toml [tool.importlinter]` layers |
