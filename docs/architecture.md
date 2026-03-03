# sonic2mod Architecture

## Overview

sonic2mod converts Sonic 1 SMPS (Sample Music Playback System) assembly music into Amiga ProTracker MOD format. The tool parses macro-based assembly text, builds an intermediate representation, then maps notes, timing, and effects into a binary MOD file.

Related docs: `docs/smps_driver.md` (Sonic 1 driver internals), `docs/pipeline.md` (effect mapping, gotchas), `docs/smps_format.md` (assembly syntax), `docs/yaml_config.md` (YAML schema).

## Data Flow

```
  .asm file
     │
     ▼
 SmpsParser.parse_file()      ← smps_parser.py
     │
     ▼
  SmpsSong (IR)               ← dataclasses in smps_parser.py
     │
     ▼
 SmpsToModConverter.convert() ← smps2mod.py
     │
     ▼
  ModFile                     ← mod.py
     │
     ▼
  .mod binary
```

## Module Descriptions

### tables.py

Foundation module with no dependencies.

- **`PERIOD_TABLE`**: 37-entry ProTracker period table (C-1 through B-3 plus a trailing 0). Periods are the Amiga Paula chip timer values that determine playback pitch.
- **`ModNote` enum**: Maps note names (C1–B3) to indices 0–35 into PERIOD_TABLE.
- **`SMPS_NOTE_NAMES` dict**: Maps all SMPS note name strings to their byte values. Built from the `_smps2asm_inc.asm` enumeration: `nRst=$80`, `nC0=$81`, 12 semitones per octave through octave 7. Includes enharmonic aliases (`nDb0`=`nCs0`, `nF0`=`nEs0`, etc.) and `nMaxPSG`=`nA5` ($C6).
- **`SMPS_DAC_NAMES` dict**: Sonic 1 DAC sample names to byte values: `dKick=$81`, `dSnare=$82`, `dTimpani=$83`, `dHiTimpani=$88`, `dMidTimpani=$89`, `dLowTimpani=$8A`, `dVLowTimpani=$8B`.
- **`smps_note_to_mod_note(note_value, transpose)`**: Computes `semitone = (note_value - 0x81) + transpose`, clamps to 0–35, returns `ModNote`.

### mod.py

MOD file writer adapted from [mml2mod-master](../mml2mod-master/mod.py).

Key changes from original:
- `CHANNELS`, `samples`, `position_list`, `patterns` moved from class variables to instance variables in `__init__(self, channels=10)`.
- `MOD_FORMAT` set dynamically from `FORMAT_TABLE` based on channel count.
- Added `set_effect(effect, param)` for writing arbitrary effects to the current channel/row.
- Added `set_position_jump(position)` for Bxx song loop effect.
- Added `create_placeholder_samples(count)` for generating silent 2-byte placeholder samples.
- `add_samples()` uses `continue` instead of `exit()` on errors (non-fatal).

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

### smps_parser.py

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
   | Hex < $80 standalone | **Update persistent duration AND create implicit rest/wait event** advancing the tick counter |
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

### config.py

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

### smps2mod.py

Conversion engine that walks the IR and writes MOD data.

#### `SmpsToModConverter.convert()` Flow

1. Set song name
2. Load samples (from file list) or create placeholders
3. Set BPM (Fxx on pattern 0, channel 0) and speed (Fxx on pattern 0, channel 1)
4. Convert all channels via `_convert_all_channels()`
5. Set song loop point from `smpsJump` via `_set_loop_point()`

#### Channel Conversion

For each configured channel, walks its event list maintaining per-channel state:

| State Variable | Updated By | Used For |
|----------------|------------|----------|
| `current_volume` | `smpsAlterVol` | Cxx volume effect |
| `alter_note` | `smpsAlterNote` | Added to transpose before note lookup |
| `note_fill` | `smpsNoteFill` | ECx note cut effect |
| `vibrato_active/speed/depth` | `smpsModSet/On/Off` | 4xy vibrato effect |
| `transpose` | `smpsChangeTransposition` | Cumulative pitch offset |

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
1. Volume change (`Cxx`) — if volume differs from channel default
2. Vibrato (`4xy`) — if modulation is active
3. Note cut (`ECx`) — if note fill is set

#### DAC Handling

DAC notes look up their `DacSampleConfig` by name. Each DAC sound maps to a specific MOD instrument number and trigger note, allowing different drum samples to be assigned to different MOD instruments.

### convert.py

CLI entry point using `argparse`.

#### Arguments

| Argument | Short | Default | Description |
|----------|-------|---------|-------------|
| `input` | — | — | Input .asm file (positional, required) |
| `--config` | `-c` | — | YAML config file |
| `--output` | `-o` | auto | Output .mod path |
| `--bpm` | — | 150 | Target BPM |
| `--speed` | — | 6 | Target speed |
| `--ticks-per-row` | — | 6.0 | SMPS ticks per MOD row |
| `--channels` | — | 10 | MOD channel count |
| `--transpose` | — | — | Global FM transpose override |
| `--name` | — | auto | Song name |

If `--config` is provided, the YAML file is loaded and CLI args override `input`/`output`. Otherwise, `default_sonic1()` creates a config from CLI args.

Output path defaults to `<input_basename>.mod` if not specified.
