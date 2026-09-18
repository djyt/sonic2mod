# sonic2mod Architecture

## Overview

sonic2mod converts Sonic 1 SMPS (Sample Music Playback System) assembly music into Amiga ProTracker MOD format. The tool parses macro-based assembly text, builds an intermediate representation, then maps notes, timing, and effects into a binary MOD file.

Related docs: `docs/smps_driver.md` (Sonic 1 driver internals), `docs/pipeline.md` (effect mapping, gotchas), `docs/smps_format.md` (assembly syntax), `docs/yaml_config.md` (YAML schema), `docs/fm_synthesis.md` (YM2612 synthesis), `docs/psg_synthesis.md` (SN76489 PSG synthesis), `docs/sfx_rendering.md` (the offline SFX driver).

## Data Flow

```
  .asm file
     │
     ▼
 SmpsParser.parse_file()      ← core/smps_parser.py
     │
     ▼
  SmpsSong (IR)               ← dataclasses in core/smps_parser.py
     │
     ▼
 SmpsToModConverter.convert() ← core/smps2mod.py   (walks channels with core/driver_state.py)
     │
     ▼
  ModFile                     ← core/mod.py
     │
     ▼
  .mod binary
```

## Layering

```
core/        parser, IR, config, the driver tables and state machine, level laws,
             the MOD writer, the shared CLI chrome, the C build and PCM helpers
  ↑
ym2612/      emulator wrappers and renderers; import core, never each other
sn76489/
  ↑
sfx/         the offline SFX driver; imports core and both chip packages
  ↑
tools/       analysis and audit utilities
*.py         the three CLIs — thin
```

`core/` is the bottom layer and imports nothing from `sfx/`, `ym2612/` or `sn76489/`.  The Sonic 1
driver tables used to live in `sfx/tables.py`, which forced the converter to import the SFX package
(by function-level imports, to dodge the cycle); they are now `core/driver_tables.py` and
`sfx/tables.py` re-exports them under the name the SFX driver has always used.

## Module Descriptions

### core/tables.py

Foundation module with no dependencies.

- **`PERIOD_TABLE`**: 37-entry ProTracker period table (C-1 through B-3 plus a trailing 0). Periods are the Amiga Paula chip timer values that determine playback pitch.
- **`ModNote` enum**: Maps note names (C1–B3) to indices 0–35 into PERIOD_TABLE.
- **`SMPS_NOTE_NAMES` dict**: Maps all SMPS note name strings to their byte values. Built from the `_smps2asm_inc.asm` enumeration: `nRst=$80`, `nC0=$81`, 12 semitones per octave through octave 7. Includes enharmonic aliases (`nDb0`=`nCs0`, `nF0`=`nEs0`, etc.) and `nMaxPSG`=`nA5` ($C6).
- **`SMPS_DAC_NAMES` dict**: Sonic 1 DAC sample names to byte values: `dKick=$81`, `dSnare=$82`, `dTimpani=$83`, `dHiTimpani=$88`, `dMidTimpani=$89`, `dLowTimpani=$8A`, `dVLowTimpani=$8B`.
- **`smps_note_to_mod_note(note_value, transpose)`**: Computes `semitone = (note_value - 0x81) + transpose`, clamps to 0–35, returns `ModNote`.
- **`semitone_to_note_name(semitone)`** / **`synth_note_name(semitone)`**: the two note spellings. The first is the driver's (index 5 is `Es`), used for SMPS labels; the second is the one YAML configs use (`F`) and is the inverse of `parse_synth_note`. Anything writing a config must emit the second — keeping them apart matters, because `Es` is a valid SMPS label and not a valid config note.

### core/driver_tables.py

A transcription of `sonic_1/s1.sounddriver.asm`, not a recomputation from music theory — the
driver's tables are what the hardware plays, and they differ from equal temperament audibly.
Self-checks against known-good assembled values run at import.

- **`FM_FREQUENCIES`** (96 entries) / **`PSG_FREQUENCIES`** (70) / **`PSG_FREQUENCIES_EXTENDED`** (128).
- **`fm_note_index` / `psg_note_index`**: note byte + transpose → table index, wrapping mod 128 as the driver does.
- **`psg_index_semitone(index)`**: the real pitch a PSG table index sounds at, including past the table's end.
- **`psg_tone2_divider(note_value, transpose)`**: the tone-2 divider a note writes — what clocks a rate-3 noise LFSR.
- **`SMPS_OP_TO_REG_OFFSET`**, **`FM_SLOT_MASK`**, **`CARRIER_OFFSETS_BY_ALG`**: the FM register layout. Read by both `ym2612/voice.py` (sample synthesis) and `sfx/chips.py` (driver emulation).
- **`PSG_ENVELOPES`**, **`PAN_VALUES`**, **`HW_FM_CHANNEL`**, **`PSG_CHANNEL`**.

### core/driver_state.py

The SMPS track state that decides an event's pitch, level and instrument. Four passes over a
channel's events used to each re-implement it — the two level pre-passes, the rate-3 divider
derivation and `_convert_channel` — and they had drifted.

- **`DriverState`**: `tl` / `att`, `hard_panned`, `transpose` (header pitch offset + every `smpsChangeTransposition`), `voice`, `instrument`, `psg_entry` / `psg_entries` / `psg_label`. Advanced one coordination flag at a time by **`apply(effect)`**; queried by `range_key`, `fm_range_entry`, `psg_ranged_entry`, `level_db`, `in_noise_mode`, `is_silent`. Built for a parsed channel with **`DriverState.for_channel(channel, config, instrument)`**, which applies the header transpose, volume and `smpsHeaderPSG` voice.
- **`source_names(song)` / `source_map(song)`**: `"DAC"`, `"FM1"…`, `"PSG1"…` in header order.
- **`chip_pitch(semitone, transpose, is_psg)`**: the real pitch the chip plays — PSG through the driver table, so notes past its ends sound as the hardware does. What `range_space: chip` matches on.
- **`pan_is_hard(params)`**, **`psg_range_entry(entries, key)`**.

State that only matters while emitting MOD data (note fill, vibrato, cursor) stays in the
converter. `core/analysis.py` keeps its own loop on purpose: it describes the song with no config
in hand, tracks volume unclamped, and names PSG tones no `psg_voice_map` mentions.

### core/levels.py

The chip level laws, in one place: `TL_STEP_DB` (0.75), `PSG_STEP_DB` (2.0),
`DEFAULT_FM_PAN_LAW_DB` (3.0), `fm_level_db`, `psg_level_db`, `db_to_mod_volume`, `fm_tl_to_mod`,
`psg_att_to_mod`, and `modal_level` (the most common level, ties to the louder one — what a
"baked" `sample_list` volume stands for). Used by the converter and by `analyze.py`'s YAML
skeleton, which therefore predict the same numbers.

### core/cli.py

Shared Rich chrome for `convert.py`, `analyze.py` and `sonic2wav.py`: `cli_console()` (with the
Windows UTF-8 stdout fix, idempotent), `branding(console, product, version)`, `row_printer`,
`error_printer`, and the `LABEL_W` label column width.

### core/cbuild.py

**`CLibrary`** — a spec (`name`, `out_dir`, `sources`, `include`, `defines`, `gcc_libs`) plus
`get_lib_path()`, which rebuilds when a source is newer than the cached `.dll` / `.so` and
compiles with gcc or MSVC. `ym2612/build.py` and `sn76489/build.py` are each ~30 lines of spec
over it.

### core/pcm.py

Helpers shared by the two synthesis pipelines: `to_mono`, `trim_trailing_silence`, `peak`,
`to_int8(mono, scale)`, `normalize_int8(mono, context)`, and `write_raw16` / `int8_to_raw16`
for the smoke tests' Audacity dumps.  They take any int sequence — the PSG path hands them
lists, the FM path the `array('i')` its C batch helper returns.

### core/mod.py

MOD file writer adapted from [mml2mod-master](../mml2mod-master/mod.py).

Key changes from original:
- `CHANNELS`, `samples`, `position_list`, `patterns` moved from class variables to instance variables in `__init__(self, channels=10)`.
- `MOD_FORMAT` set dynamically from `FORMAT_TABLE` based on channel count.
- Added `set_effect(effect, param)` for writing arbitrary effects to the current channel/row.
- Added `set_position_jump(position)` for Bxx song loop effect.
- Added `create_placeholder_samples(count)` for generating silent 2-byte placeholder samples.
- `add_samples()` uses `continue` instead of `exit()` on errors (non-fatal).
- Added the cell-addressing helpers `cell_index(row, channel)`, `ensure_pattern(index)`,
  `effect_at(pattern, row, channel)`, `note_at(...)`, `effect_slot_free(...)` and
  `free_effect_channel(pattern, row, order=None)`, plus the module-level `row_to_bcd(row)`.
  These are the only place that knows a cell is 4 bytes at `channel*4 + row*CHANNELS*4`; the
  converter used to compute that stride itself in four places and `apply_pattern_breaks` in two.
  Note that `set_active_pattern` already grows the pattern list, so callers need no
  `add_patterns` loop before it.

Classes:
- **`ModSample`**: 30-byte sample header + raw PCM data. Fields: name (22 bytes), length (words), finetune, volume, repeat offset, repeat length.
- **`ModPattern`**: Raw bytearray of `channels * 4 * 64` bytes. Each note entry is 4 bytes encoding period, instrument, effect, and effect parameter.
- **`ModFile`**: Assembles the complete MOD binary. Manages active pattern/channel/row state for note placement.

MOD binary layout:
```
Offset  Size   Content
0       20     Song name (null-terminated ASCII)
20      930    31 sample headers (30 bytes each)
950     1      Number of positions used
951     1      Song length (127)
952     128    Position list (pattern indices)
1080    4      Format tag ("M.K.", "10CH", etc.)
1084    N*P    Pattern data (N=channels*4*64 per pattern, P patterns)
...            Sample PCM data (concatenated)
```

### core/smps_parser.py

The core parser. Converts SMPS assembly text into an intermediate representation.

#### IR Data Classes

| Class | Purpose |
|-------|---------|
| `SmpsNote` | Single note/rest/DAC event with value, duration, flags |
| `SmpsEffect` | Effect macro with type string and parameter list |
| `SmpsEvent` | Union wrapper (note or effect) with cumulative tick position |
| `SmpsChannelHeader` | Channel metadata from header macros |
| `SmpsSongHeader` | Voice label, channel counts, tempo |
| `SmpsChannel` | Header + ordered event list + jump info |
| `SmpsVoice` | FM voice definition (algorithm, feedback, raw params) |
| `SmpsSong` | Top-level container for header, channels, voices |

#### Parsing Stages

1. **`_preprocess(text)`**: Strip `;` comments, blank lines, normalize whitespace.

2. **`_collect_labels()`**: First pass — map label names (lines ending with `:`) to line indices.

3. **`_parse_header()`**: Extract `smpsHeaderStartSong`, `smpsHeaderVoice`, `smpsHeaderChan`, `smpsHeaderTempo`, `smpsHeaderDAC`, `smpsHeaderFM`, `smpsHeaderPSG` macros using regex. FM/PSG pitch offsets are interpreted as signed bytes.

4. **`_parse_channel_data(ch_header)`** → **`_parse_channel_lines()`**: Walk lines from the channel's start label, producing events. Key behaviors:
   - **Fall-through**: Does NOT stop at label boundaries. Only `smpsStop` and `smpsJump` terminate parsing. This handles the FM5→FM1 shared data pattern in Title Screen.
   - **`stop_line` parameter**: Used by loop unrolling to prevent re-entering the `smpsLoop` command.

5. **`_parse_dcb_line()`**: Tokenizes `dc.b` lines (comma-separated). Token classification:

   | Token | Action |
   |-------|--------|
   | Note name (`nC3`, `nRst`) | Create pending note, await duration |
   | DAC name (`dKick`) | Create pending DAC note, await duration |
   | Hex < $80 after note | Assign as duration to pending note, emit event |
   | Hex < $80 standalone | **Update persistent duration AND emit a continuation event**: without preceding `smpsNoAttack`, retriggers last note (`is_rest=False, note_value=last_note_value`); with `smpsNoAttack` pending, emits a rest/sustain (`is_rest=True, is_no_attack=True`) |
   | `smpsNoAttack` / `$E7` | Flag next note as no-attack |
   | Hex >= $80 | Interpret as raw note byte (rest=$80, note=$81+) |

   The standalone duration behavior is critical: in SMPS, a bare duration byte means "wait/sustain for N ticks." Without this, loops like PSG3's pattern (alternating `smpsNoteFill` + `dc.b $0C`) would not advance time correctly.

6. **Loop unrolling** (`smpsLoop $idx, $count, label`): The first play-through of the loop body happens naturally during linear parsing. On encountering the `smpsLoop` command, the parser replays lines from the target label to the loop command line (`stop_line=i`) for `count - 1` additional iterations.

7. **Call inlining** (`smpsCall label`): Follows the target label, collects events until `smpsReturn`, splices them into the channel's event list.

8. **`_parse_voices(voice_label)`**: Parses `smpsVcAlgorithm`, `smpsVcFeedback`, and other `smpsVc*` macros into `SmpsVoice` objects. These are informational (not directly mapped to MOD instruments).

#### Recognized Effect Macros

| Macro | Parsed As | Parameters |
|-------|-----------|------------|
| `smpsSetvoice` / `smpsFMvoice` | `smpsSetvoice` | voice index |
| `smpsAlterVol` | `smpsAlterVol` | signed delta |
| `smpsAlterNote` / `smpsDetune` | `smpsAlterNote` | signed semitones |
| `smpsModSet` | `smpsModSet` | wait, speed, depth, steps |
| `smpsModOn` | `smpsModOn` | — |
| `smpsModOff` | `smpsModOff` | — |
| `smpsNoteFill` | `smpsNoteFill` | fill value |
| `smpsPan` | `smpsPan` | direction string |
| `smpsNop` | `smpsNop` | value |
| `smpsPSGform` | `smpsPSGform` | waveform byte |
| `smpsPSGvoice` | `smpsPSGvoice` | voice name |
| `smpsChangeTransposition` | `smpsChangeTransposition` | signed semitones |

### core/config.py

Per-song conversion configuration with YAML loading.

#### `ConversionConfig` Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | "Untitled" | MOD song name (max 19 chars) |
| `input_file` | str | "" | Input .asm path |
| `output_file` | str | "output.mod" | Output .mod path |
| `target_bpm` | int | 150 | BPM (32–255), set via Fxx effect |
| `target_speed` | int | 6 | Ticks per row (1–31), ProTracker default is 6 |
| `ticks_per_row` | float | 6.0 | SMPS ticks per MOD row (controls time scaling) |
| `num_mod_channels` | int | 10 | MOD channel count (4/8/10/12/14/16) |
| `channels` | list | [] | Per-channel `ChannelConfig` entries |
| `dac_samples` | list | [] | DAC→instrument/note mappings |
| `sample_list` | list\|None | None | Raw sample file entries `[inst, filename, vol, finetune]` |
| `samples_dir` | str | "./samples/" | Base path for sample files |
| `max_patterns` | int | 127 | Truncation limit |

#### `ChannelConfig` Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | str | — | SMPS source: `DAC`, `FM1`–`FM5`, `PSG1`–`PSG3` |
| `mod_channel` | int | — | 0-based MOD channel index |
| `transpose` | int | 0 | Semitone offset applied during conversion |
| `instrument` | int | 1 | MOD instrument number (1–31) |
| `volume` | int | 64 | MOD volume (0–64) |
| `enabled` | bool | True | Skip channel if false |

#### `DacSampleConfig` Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | — | SMPS DAC name (e.g. `dKick`) |
| `mod_instrument` | int | — | MOD instrument to trigger |
| `mod_note` | str | "C3" | Note to write in MOD pattern |

#### `default_sonic1()` Defaults

- 10 channels: DAC→ch0, FM1–5→ch1–5, PSG1–3→ch6–8
- FM transpose: -36 (maps SMPS octaves 3–5 to MOD C1–B3)
- PSG transpose: -36
- DAC samples: dKick→inst 1, dSnare→inst 7, dTimpani→inst 8, timpani variants→inst 9–12

### core/smps2mod.py

Conversion engine that walks the IR and writes MOD data.

#### `SmpsToModConverter.convert()` Flow

1. Set song name
2. Optionally run `generate_fm_samples()` (ym2612/) and `generate_psg_samples()` (sn76489/) to synthesize PCM
3. Load samples (from file list) or create placeholders
4. Set BPM (Fxx on pattern 0, channel 0) and speed (Fxx on pattern 0, channel 1)
5. Re-time every channel for `smpsSetTempoDiv` (`_apply_global_tempo_div()`), then extend short loop bodies (`_extend_looping_channels()`)
6. Convert all channels via `_convert_all_channels()`, which first plans the baked levels
7. Write mid-song `smpsSetTempoMod` changes (`_write_tempo_changes()`)

`_set_loop_point()` is called by `convert.py` afterwards - after `apply_pattern_breaks`, so the
`Bxx` lands at the right post-break position (see the call-order gotcha in `CLAUDE.md`).

#### Channel Conversion

Every pass over a channel's events - `_convert_channel`, the `_plan_levels` level pre-passes and
`_derive_rate3_dividers` - walks with the same `DriverState` (`core/driver_state.py`), so they
cannot disagree about what a note plays:

| DriverState field | Updated by | Used for |
|-------------------|------------|----------|
| `tl` / `att` | `smpsAlterVol`, starting from the header volume | the note's level, hence `Cxx` |
| `hard_panned` | `smpsPan` | the -3 dB pan term in the FM level |
| `transpose` | `smpsChangeTransposition`, starting from the header pitch offset | pitch, and the `range_space: chip` lookup |
| `voice` | `smpsSetvoice` | `voice_map` / `channel_instrument_map` lookup |
| `psg_entry` / `psg_entries` / `psg_label` | `smpsPSGform` to `psg_map`, `smpsPSGvoice` to `psg_voice_map`, and the `smpsHeaderPSG` voice | PSG instrument, root anchoring, warning context |
| `instrument` | all of the above | the MOD instrument a note lands on |

`_convert_channel` keeps only the state the driver knows nothing about:

| State Variable | Updated By | Used For |
|----------------|------------|----------|
| `current_volume` | `smpsAlterVol` | `Cxx` in the non-baked volume modes only |
| `note_fill` | `smpsNoteFill` | `ECx` / `C00` note cut |
| `vibrato_active/speed/change/steps/wait` | `smpsModSet/On/Off` | `4xy` vibrato |
| `last_note_cell` | each note-on | keeps two note-ons out of one cell |

`smpsAlterNote` / `smpsDetune` updates nothing: it is a raw FNUM offset (about 10 cents), not
semitones, and affects neither pitch placement nor range lookup.

#### Tick-to-Pattern/Row Conversion

```
row_total = int(tick / ticks_per_row)
pattern   = row_total // 64
row       = row_total % 64
```

With `ticks_per_row=6`:
- SMPS duration $06 (6 ticks) = 1 row
- SMPS duration $0C (12 ticks) = 2 rows
- SMPS duration $18 (24 ticks) = 4 rows

#### Effect Priority

Only one effect per note per row. Priority order (first match wins):
1. Volume change (`Cxx`) - if the note's level differs from its instrument's baked level
2. Vibrato (`4xy`) - if modulation is active
3. Note cut (`ECx`) - if note fill is set

A note that starts between rows takes `EDx` on the row it starts in when the slot is free, which
displaces an attack-row `4xy`; a displaced `Cxx` moves to the note's next free row. Full rules:
`docs/pipeline.md` section "Notes that start between rows".

#### DAC Handling

DAC notes look up their `DacSampleConfig` by name. Each DAC sound maps to a specific MOD instrument number and trigger note, allowing different drum samples to be assigned to different MOD instruments.

### convert.py

CLI entry point using `argparse`.

#### Arguments

| Argument | Short | Default | Description |
|----------|-------|---------|-------------|
| `config` | — | — | YAML config file (positional, required) |
| `--output` | `-o` | auto | Output .mod path |

YAML config is the required positional argument. `--output` overrides `output_file` from YAML.

Output path defaults to `<input_basename>.mod` if not set in YAML.

Console chrome (branding panel, label column, error printer) comes from `core/cli.py`, shared with
`analyze.py` and `sonic2wav.py`.

The converter reports through two public lists, `SmpsToModConverter.warnings` and `.infos`, each
holding dicts with a `type` key. `convert.py` renders the informational ones inline and dispatches
warnings through `_WARNING_RENDERERS`, a `{type: function}` table - a new warning type is one
function and one entry, and an unrecognised type prints nothing.

---

## sn76489/ — SN76489 PSG Synthesis Package

Synthesizes SN76489 PSG samples (tone and noise) from `psg_map`/`psg_voice_map` config entries.
Full reference: `docs/psg_synthesis.md`.

### build.py

Auto-compiles `reference/SN76489/sn76489.c` + `panning.c` → `sn76489/sn76489.dll` (Windows) or `sn76489.so` (Unix). Rebuilds only when C sources are newer than the compiled library. The compile itself is `core.cbuild.CLibrary`, shared with `ym2612/build.py`; this module is only the spec.

### wrapper.py — `SN76489` class

ctypes wrapper around the VGMPlay SN76489 emulator. Methods:

- `write_tone_freq(ch, n)` — set tone channel ch (0–2) frequency divider N
- `write_volume(ch, vol)` — set channel volume (0=max, 15=silent)
- `write_noise(white, rate)` — configure noise LFSR (white/periodic, rate 0–3)
- `render_samples(n) → list[(L,R)]` — render n stereo INT32 sample pairs
- `shutdown()` — free chip context

**Critical:** `SN76489_Reset` must be called explicitly after `SN76489_Init`; the C source has it commented out. `wrapper.py` calls it in `__init__`.

### renderer.py

Converts MOD note indices and noise configs to 8-bit signed mono PCM:

- `render_psg_tone(mod_note_index, ...) → (bytes, int)` — tone → PCM + sample rate
- `render_psg_noise(white, noise_rate, ...) → (bytes, int)` — noise → PCM + sample rate
- `note_to_psg_n(mod_note_index, clock_rate) → int` — note → 10-bit SN76489 divider N
- `*_raw` variants return `(list[int], int)` before normalization (used by `sample_generator.py`)

### sample_generator.py

```python
generate_psg_samples(config, psg_synth, verbose=False) → dict[int, tuple[bytes, int]]
```

Iterates all `PsgInstrumentEntry` objects from `config.psg_map` and `config.psg_voice_map`,
synthesizes each, applies global normalization (`127.0 / psg_output_max`, through
`core.pcm.to_int8`), and returns a `{inst_num: (pcm_bytes, sample_rate_hz)}` dict ready for
`ModFile` insertion.

### validate.py

Standalone smoke test: `python sn76489/validate.py` renders a C3 tone and white noise,
writing `output/psg_tone_test.raw` and `output/psg_noise_test.raw` (16-bit for Audacity).
