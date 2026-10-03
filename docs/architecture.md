# sonic2mod Architecture

## Overview

sonic2mod converts Sonic 1 SMPS (Sample Music Playback System) assembly music into Amiga ProTracker MOD format. The tool parses macro-based assembly text, builds an intermediate representation, then maps notes, timing, and effects into a binary MOD file.

Related docs: `docs/smps_driver.md` (Sonic 1 driver internals), `docs/pipeline.md` (effect mapping, gotchas), `docs/smps_format.md` (assembly syntax), `docs/yaml_config.md` (YAML schema), `docs/fm_synthesis.md` (YM2612 synthesis), `docs/psg_synthesis.md` (SN76489 PSG synthesis), `docs/sfx_rendering.md` (the offline SFX driver).

## Data Flow

```
  .asm file                          .vgm / .vgz rip
     │                                   │
     ▼                                   ▼
 SmpsParser.parse_file()          read_vgm → lift_song()   ← core/vgm/ (the lift: Phase 1 of
  ← core/smps/parser.py              │                        docs/todo/vgz_conversion.md)
     └──────────┬────────────────────┘
                │   read_song(path) / ConversionConfig.read_song()   ← core/source/ picks by suffix
                ▼
  SmpsSong (IR)               ← dataclasses in core/smps/song.py
     │
     ▼
 SmpsToModConverter.convert() ← core/convert/smps2mod.py   (walks channels with core/plan/driver_state.py)
     │
     ▼
  ModFile                     ← core/mod/file.py
     │
     ▼
  .mod binary
```

## Layering

```
*.py          the three CLIs — thin
tools/        analysis and audit utilities
  ↓
sfx/          the offline SFX driver; imports core and both chip packages
  ↓
ym2612/       emulator wrappers and renderers; import core (plan, config, smps, audio), never each other
sn76489/
  ↓
core/
  ui/         report, pitch-audit report, CLI chrome
  convert/    SmpsToModConverter and its passes            ── calls the chip packages through generators.py
  audit/      a converted MOD against its VGZ, symbolically (pitch per chip note; the tools' library)
  merge/      channel folding, composites, banks
  plan/       the song read through its config: DriverState walk, instrument catalogue, detune,
              synthesis pitches, noise derivations, timeline
  config/     song configs, settings.yaml
  source/     a song file -> SmpsSong: asm parsed (smps/), a VGM rip lifted (vgm/)
  mod/  vgm/  smps/  chips/  audio/
              the MOD format · VGM register logs · the SMPS source and driver · the two sound
              chips (no driver) · sample arithmetic (no SMPS, no MOD)
  diagnostics.py, analysis.py, cbuild.py, version.py
```

Inside `core/` a package imports only packages below it (`mod` uses `audio`; `vgm` uses `smps`;
`smps` and `audio` nothing); `diagnostics.py` has no imports and any package may report through it.  Each package's
`__init__.py` exports what the other packages import (`from ..smps import SmpsSong`), so a module
can move inside its package without its importers knowing; a package's own modules import each
other directly (`from .song import SmpsNote`).

`core/` imports nothing from `sfx/`, `ym2612/` or `sn76489/`.  The Sonic 1
driver tables used to live in `sfx/tables.py`, which forced the converter to import the SFX package
(by function-level imports, to dodge the cycle); they are now `core/smps/driver_tables.py` and
`sfx/tables.py` re-exports them under the name the SFX driver has always used.

The converter renders its samples through the chip packages without importing them:
`core/convert/generators.py` states the two generators it calls (`FmGenerator`, `PsgGenerator`) and
`convert.py` hands the implementations in as `SampleGenerators(fm=generate_fm_samples,
psg=generate_psg_samples)`.  A converter given none (the tools that only walk the song) raises
if asked to synthesise.

## Module Descriptions

### core/smps/names.py

No dependencies.

- **`SMPS_NOTE_NAMES` dict**: Maps all SMPS note name strings to their byte values. Built from the `_smps2asm_inc.asm` enumeration: `nRst=$80`, `nC0=$81`, 12 semitones per octave through octave 7. Includes enharmonic aliases (`nDb0`=`nCs0`, `nF0`=`nEs0`, etc.) and `nMaxPSG`=`nA5` ($C6).
- **`SMPS_DAC_NAMES` dict**: Sonic 1 DAC sample names to byte values: `dKick=$81`, `dSnare=$82`, `dTimpani=$83`, `dHiTimpani=$88`, `dMidTimpani=$89`, `dLowTimpani=$8A`, `dVLowTimpani=$8B`.
- **`semitone_to_note_name(semitone)`** / **`synth_note_name(semitone)`**: the two note spellings. The first is the driver's (index 5 is `Es`), used for SMPS labels; the second is the one YAML configs use (`F`) and is the inverse of `parse_synth_note`. Anything writing a config must emit the second — keeping them apart matters, because `Es` is a valid SMPS label and not a valid config note.
- **`source_names(song)` / `source_map(song)`**: `"DAC"`, `"FM1"…`, `"PSG1"…` in header order.

### core/smps/driver_tables.py

A transcription of `sonic_1/s1.sounddriver.asm`, not a recomputation from music theory — the
driver's tables are what the hardware plays, and they differ from equal temperament audibly.
Self-checks against known-good assembled values run at import.

- **`FM_FREQUENCIES`** (96 entries) / **`PSG_FREQUENCIES`** (70) / **`PSG_FREQUENCIES_EXTENDED`** (128).
- **`fm_note_index` / `psg_note_index`**: note byte + transpose → table index, wrapping mod 128 as the driver does.
- **`psg_index_semitone(index)`**: the real pitch a PSG table index sounds at, including past the table's end.
- **`chip_pitch(semitone, transpose, is_psg)`**: the real pitch the chip plays — PSG through the driver table, so notes past its ends sound as the hardware does. What `range_space: chip` matches on.
- **`psg_tone2_divider(note_value, transpose)`**: the tone-2 divider a note writes — what clocks a rate-3 noise LFSR.
- **`SMPS_OP_TO_REG_OFFSET`** (the order the driver stores a voice's operators), **`FM_SLOT_MASK`** (the driver's FMSlotMask, checked against `core.chips.CARRIER_OFFSETS_BY_ALG`). Read by both `ym2612/voice.py` (sample synthesis) and `sfx/chips.py` (driver emulation).
- **`PSG_ENVELOPES`** (with `$80` terminators, for the SFX driver) and **`PSG_ENVELOPES_BY_NAME`**
  (`fTone_01` … `fTone_09` without them, what a config's `envelope:` resolves to), **`PAN_VALUES`**,
  **`HW_FM_CHANNEL`**, **`PSG_CHANNEL`**.

### core/chips/

The two sound chips' own facts, with no driver in them: `fm.py` (YM2612: `MD_FM_CLOCK`, `FM_SAMPLE_RATE`,
`CARRIER_OFFSETS_BY_ALG`, `carrier_names`, `fm_frequency_hz`, and the level laws `TL_STEP_DB` (0.75),
`DEFAULT_FM_PAN_LAW_DB` (3.0), `fm_level_db`) and `psg.py` (SN76489: `MD_PSG_CLOCK`, `PSG_SAMPLE_RATE`,
`psg_frequency_hz`, `PSG_STEP_DB` (2.0), `psg_level_db`).  `smps/driver_tables.py` builds its tables on
them and checks at import that the driver's `FMSlotMask` names the same carriers; `vgm/` reads the chips
through them, so only `vgm/lift/` imports `smps/` (the song it produces) - `tests/test_chips_units.py`
pins both.  The level laws are used by the converter and by `analyze.py`'s YAML skeleton, which therefore
predict the same numbers (`vgm_analyze` reads its chip levels through them too).  dB → MOD volume is
`core/mod/volume.py` (`db_to_mod_volume`, `clamp_mod_volume`, `headroom_db`); the two together, the absolute
volume modes' `fm_tl_to_mod` / `psg_att_to_mod` and `modal_level` (the most common level, ties to the louder
one — what a "baked" `sample_list` volume stands for), are `core/convert/level_plan.py`'s.

### core/smps/parser.py

The core parser. Converts SMPS assembly text into an intermediate representation.

#### IR Data Classes (core/smps/song.py)

| Class | Purpose |
|-------|---------|
| `SmpsNote` | Single note/rest/DAC event with value, duration, flags |
| `SmpsEffect` | A coordination flag (`CoordFlag`, keyed by its driver byte) and its parameters; `PAN`'s is the B4 byte.  The SMPS2ASM macro names are `names.py`'s (`flag_name`, `flag_from_macro`): the parser reads them, `analyze.py` prints them, nothing else uses them |
| `SmpsEvent` | Union wrapper (note or effect) with cumulative tick position |
| `SmpsChannelHeader` | Channel metadata from header macros |
| `SmpsSongHeader` | Voice label, channel counts, tempo |
| `SmpsChannel` | Header + ordered event list + jump info |
| `SmpsVoice` | FM voice: algorithm, feedback, and `operators` - each `VoiceField` (DT, MUL, KS, AR, AM, D1R, D2R, D1L, RR, TL) as four ints in the driver's operator order.  The `smpsVc*` spellings are `names.py`'s (`voice_field_from_macro`).  `registers(tl_offset)`: the operator registers as the driver writes them (what `program_voice` writes, what a frame log holds) |
| `SmpsSong` | Top-level container for header, channels, voices |

#### What a song plays (core/smps/track.py, tempo.py, playback.py, compare.py)

`tempo.py`: `TempoSegment` (a stretch of one tempo modifier: the frame each tick is read on, its
holds) and `tempo_schedule(modifier, changes)` from a song's start.  The playback walk and the VGM
lift both use it.

`TrackState` is one track's driver state as its flags leave it (transpose, TL offset / attenuation,
pan, detune, voice, envelope, noise form, note fill, modulation), config-free; `DriverState`
(core/plan) adds the MOD routing to it.  `played_song(song)` walks every channel with it after
`song_prep` and gives each note as the driver plays it (`PlayedNote`: ticks, attack, the frequency
table note and the frequency word written, the voice's registers but the carriers' TL, the
carriers' TL, pan, modulation, fill, noise byte, DAC sample), consecutive rests merged, a note
`smpsNoteFill` keys off before the next read a note and a rest, a held duration a tie, a stopped
track resting to the song's end.  `compare_songs(expected, got, aspects)` matches
two songs' notes by start tick and reports per `Aspect`: the yardstick a VGM lift is accepted by
(`tools/vgm_lift.py`), blind to how the asm spells a note.

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

### core/mod/notes.py

No dependencies.

- **`PERIOD_TABLE`**: 37-entry ProTracker period table (C-1 through B-3 plus a trailing 0). Periods are the Amiga Paula chip timer values that determine playback pitch.
- **`ModNote` enum**: Maps note names (C1–B3) to indices 0–35 into PERIOD_TABLE.
- **`MOD_NOTE_MAP`**: a config's MOD note spellings (`C2`, `Fs3`, `F#3`, `Gb3`) → `ModNote`.

### core/mod/file.py

MOD file writer adapted from [mml2mod-master](../mml2mod-master/mod.py), and the one reader:
`read_mod(bytes | path)` → `ModImage` (tag, channels, `SampleInfo` headers with their data, the
order, every stored pattern's cells), `ModImage.play_rows()` (one pass in play order, `Bxx` /
`Dxx` followed left to right), `format_channels(tag)` (every tag the writer emits, `14CH`
included) and `isolate_channel` (one channel's notes, the flow commands kept: what the render
audits play).  `core/mod/sample_audit.py`, `tools/mod_compare.py`, `tools/mod_lint.py`,
`tools/vgm_compare.py`, `tools/vgm_pitch_audit.py` and `tools/mod_render_diff.py` read with it.

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

### core/mod/timing.py

`timed_pass(mod, speed)`: each row `play_rows` plays with its start in seconds and the BPM in force
(`TimedRow`), and the pass's length — `Fxx` sets speed / BPM for its whole row, a tick lasting
2.5 / BPM s.  `edx_delay(eff, par, bpm)`: how far into its row an `EDx` note starts.  The MOD side
of the VGZ audits (`vgm_pitch_audit.mod_timeline`, `vgm_compare.mod_note_events` /
`mod_pattern_spans`) times its notes with these, so both share one clock.

### core/vgm/

A VGM / VGZ register log, the source when there is no disassembly.  Beside `smps/`, whose tables
it reads; inside `core/` only `source/` imports it.

```
reader.py     commands -> VgmLog: header (clocks, rate), writes [VgmWrite(sample, op, port, reg,
              value)], the PCM bank (type-0 data blocks), loop sample, GD3 tags.  No interpretation:
              a 0x8n command is logged as the YM2612 0x2A write it is.  Unknown commands raise VgmError
chipstate.py  ChipState.replay(log): registers write by write, yielding a Change (FM key / frequency,
              PSG tone / volume / noise, DAC byte, PCM seek) where one is musically visible; the
              accessors read the rest (carrier TLs, algorithm, pan, operator registers).  Chip
              semantics: A4 latched until A0 (one latch for the chip, as Nuked-OPN2), a PSG period
              latch followed by its data byte at the same sample is one change
frames.py     frame_log(log): the log cut into V-int frames (FrameLog, Frame per frame: FmFrame x 6,
              PsgFrame x 4, DacFrame).  A frame's window opens a quarter frame before the burst phase
              (the commonest burst start, found from the writes): on all 19 Sonic 1 rips every burst
              lands in one frame and no frame holds two
notes.py      note_starts(log): NoteStart per FM key-on (a re-key within mod_cents of the keyed pitch is a
              tie), PSG channel turning audible or leaving its note's pitch, noise turning audible, PCM
              seek; NoteTracker applies the rules change by change.  pitch_segments(log): each FM / PSG
              tone channel's sounding pitch as change points (vgm_pitch_audit's chip timeline)
cache.py      load_frames(path, cache_dir): a rip's FrameLog kept in samples.render_cache under a hash
              of the file and of the code that makes it (0.06 s for Green Hill instead of 0.65)
lift/         lift_song(frames, LiftOptions) -> SmpsSong (Phase 1 of docs/todo/vgz_conversion.md):
  tracks.py     each track's hits by frame: FM key writes (attack after a key-off, else a tie or
                legato), PSG notes where the pitch jumps or the level rises (a rip logs only
                changes), DAC seeks at the burst they follow; pitch the nearest table note
  tempo.py      infer_tempo: the TempoMap (core.smps.TempoSegment per tempo, missed V-ints) that
                puts every FM key-on on a tick in the fewest bits; tempo changes by a DP
  song.py       lift_song: hits -> ticks -> SmpsChannels; smpsSetTempoMod on FM1; every track loops
                where the rip does; the header's divider the FM notes' grid
```

`frames.py` counts the DAC's byte stream without replaying it (most of a log's writes): no state
reads register 0x2A.

`tools/vgm_analyze.py` (key-on rows, `--frames`), `tools/vgm_pitch_audit.py` (`chip_timeline`) and
`tools/vgm_compare.py` read through it; before 2026-10-03 the first two each parsed the command
stream themselves.

### core/source/

`read_song(path, LiftOptions | None)`: a `.vgm` / `.vgz` path is lifted, anything else parsed by
`SmpsParser` (an asm with a `driver:` other than `sonic1` is refused).  `ConversionConfig.read_song()`
calls it with the config's `driver:` / `tempo_modifier:` / `tempo_divider:`; `convert.py`,
`analyze.py`, `merge_survey.py`, `config_to_chip_space.py` and `vgm_pitch_audit.py` read their song
through one or the other.

### core/audit/

`pitch.py`: the symbolic pitch audit `vgm_pitch_audit.py` and `vgm_compare.py` share — `prepare_audit`
(the instruments as `prepare_instruments` leaves them), `mod_pitch_timeline` (each MOD note's pitch from
`sounding_pitches`, E1x / E2x followed), `note_start_offset` (the lag, from note starts), `audit_pitches`
(ok / wrong / missing per channel, the per-instrument verdict).  Its report is `core/ui/pitch_audit.py`.
The rendered comparison's library is here too, `tools/vgm_compare.py` keeping its reports and CLI:

```
render.py   VGMPlay (mute masks, VGM_CHANNELS) and ffmpeg + libopenmpt renders, one channel each
signal.py   WAV loading, level, spectra, partials, onsets, envelope alignment, pitch tracks, vibrato
levels.py   per-instrument level error, the sample_list volumes that zero it (write_volumes)
onsets.py   chip key-ons / audio onsets paired with the MOD's, one to one
```

Beside `convert/`: it reads `plan`, `config`, `mod` and `vgm`, and nothing reads it but the tools.

### core/audio/gain.py

`db_to_gain`, `gain_to_db`, `power_to_db`: the dB conversions every module and tool uses.

### core/audio/pcm.py

Helpers shared by the two synthesis pipelines: `to_mono`, `trim_trailing_silence`, `peak`,
`to_int8(mono, scale)`, `normalize_int8(mono, context)`, and `write_raw16` / `int8_to_raw16`
for the smoke tests' Audacity dumps.  What a MOD sample may hold (`MAX_MOD_SAMPLE_BYTES`,
`sample_limit_bytes`, `max_sustain_secs`) is `core/mod/limits.py`.  They take any int sequence — the PSG path hands them
lists, the FM path the `array('i')` its C batch helper returns.

### core/audio/resample.py

Polyphase Kaiser-windowed sinc resampler (32 taps, 512 phases, >70 dB stopband), standard
library only.  The SFX renderer takes 53267 Hz to 44100 Hz through it and the FM sample
pipeline takes 53267 Hz to each instrument's MOD target rate; `sfx/resample.py` re-exports it
under the name the SFX driver uses.

### core/config/

Per-song conversion configuration and the global synthesis settings; the package exports every
name other modules import (`from core.config import ConversionConfig, SynthesisSettings`).

```
loader.py    load_yaml (no key given twice), read_yaml_file, the value words (mode_word, dither_mode)
entries.py   InstrumentRange, PsgInstrumentEntry, ChannelConfig, DacSampleConfig, MergeGroup and
             their parsers, one per song-config section (parse_channels, parse_psg_map, ...)
song.py      ConversionConfig; from_yaml reads each section with its entries.py parser
settings.py  SampleSettings (amiga_clock, samples:, the resolved sustain) and its two chips:
             SynthesisSettings (YM2612, legato, player), PsgSynthesisSettings (SN76489);
             find_settings / load_settings: the file every CLI and tool reads (beside the config, else
             configs/settings.yaml) - a value it states is used everywhere, a constant only when it is missing
bpm.py       derive_bpm, exact_bpm, bpm_rounding_options
```

#### `ConversionConfig` Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | "Untitled" | MOD song name (max 19 chars) |
| `input_file` | str | "" | Input .asm path, or a .vgm / .vgz rip (lifted) |
| `driver` | SmpsDriver | sonic1 | The SMPS variant that played the song |
| `tempo_modifier` / `tempo_divider` | int\|None | None | VGM input only: override the lift's inferred tempo |
| `output_file` | str | "output.mod" | Output .mod path |
| `target_bpm` | int | 150 | BPM (32–255), set via Fxx effect |
| `target_speed` | int | 6 | Ticks per row (1–31), ProTracker default is 6 |
| `ticks_per_row` | float | 6.0 | SMPS ticks per MOD row (controls time scaling) |
| `num_mod_channels` | int\|None | None | MOD channel count override (4/8/10/12/14/16); `mod_channel_count` derives it from `channels` when unset |
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

### core/plan/driver_state.py

The SMPS track state that decides an event's pitch, level and instrument. Four passes over a
channel's events used to each re-implement it — the two level pre-passes, the rate-3 divider
derivation and `ChannelWriter` — and they had drifted.

- **`DriverState`** (a `core.smps.TrackState`): `tl` / `att`, `hard_panned`, `transpose` (header pitch offset + every `smpsChangeTransposition`), `detune` (`smpsDetune`), `voice`, `envelope`, `noise_form`, `fill`, `modulation`; its own: `instrument`, `psg_entry` / `psg_entries` / `psg_label`. Advanced one coordination flag at a time by **`apply(effect)`**; queried by `range_key`, `fm_ranges` / `fm_range_entry`, `level_db`, `in_noise_mode`, `is_silent`. Built for a parsed channel with **`DriverState.for_channel(channel, config, instrument)`**, which applies the header transpose, volume and `smpsHeaderPSG` voice.
- **`resolve_note(st, source_semitone, chan_transpose, source)` → `ResolvedNote`**: the one place that says which MOD instrument a pitched note is routed to and which MOD note it triggers (`instrument`, `index`, `raw_index` before clamping, `path` = `fm_root` / `psg_root` / `psg_fixed` / `transpose`, the `entry` that routed it, `chip` pitch, `detune`).
- **`walk_channel(channel, config, chan_cfg, st=None)`**: yields `(event, state, resolved)` for every event, the state advanced past each flag before it is yielded and every pitched note resolved. With a merge plan on the config (`convert.py --merged`) the resolved instrument is the composite where one plays. **`enabled_channels(song, config, kinds)`** yields `(chan_cfg, channel)` for the per-kind loops. The conversion, its level and sustain pre-passes, the noise / rate-3 derivations, `resolve_synth_roots` (synth_roots.py) and `core/merge/` all walk this way.
- **`psg_range_entry(entries, key)`**: the entry of a multi-range `psg_voice_map` list covering a note.

State that only matters while emitting MOD data (note fill, vibrato, cursor) stays in the
converter. `core/analysis.py` keeps its own loop on purpose: it describes the song with no config
in hand, tracks volume unclamped, and names PSG tones no `psg_voice_map` mentions.

### core/plan/instruments.py

The instrument catalogue: `{MOD instrument: FmInstrument | PsgInstrument}` in render order, the
first map entry to name an instrument deciding what its sample is rendered for (Credits folds
voices onto 31 slots; Stage Clear's PSG2 plays two octaves up its PSG1 sample).
`fm_catalogue(song, config)` walks voice_map (voices the song defines), channel_instrument_map
(rooted), channel_instrument_map (rootless, played at C1), then the merge plan's
composites; `psg_catalogue(config, noise_envelopes)` walks psg_map (each entry, then its
`envelopes:` variants) and psg_voice_map. An `FmInstrument` is a list of **`FmLayer`**s (voice,
semitone offset, FNUM detune, carrier TL offset relative to the instrument's render level); a
plain instrument has one layer, a composite one per folded channel; a detune variant
(core/plan/detune.py) is its base with the layer's FNUM detune set. `generate_fm_samples`,
`generate_psg_samples` and the converter's `SustainPlanner._synthesis_roots` / `SustainPlanner._needs` all read it.
`free_slots(config, song)`: the slots nothing names (detune variants, then composites, take them).

### core/plan/detune.py

Detune variants: `plan_detune_variants(song, config)` counts every FM note's (instrument,
`smpsAlterNote` detune), renders each instrument at its majority detune and gives every other
a free slot (`DetunePlan`, on `config.detune_plan`): `resolve_note` routes to it, the catalogue
renders it, the level plans share the base's.  `detune_cents(semitone, offset)` is the interval
an offset makes on the driver's frequency table.

### core/plan/instrument_plan.py

`prepare_instruments(song, config, synth)`: `resolve_synth_roots`, then `plan_detune_variants` where the
settings want variants — the converter's first steps (it reports what they return) and the whole
preparation `vgm_pitch_audit` / `vgm_compare` make, so the audits see the instruments the converter
rendered.  `sounding_pitches(song, config)`: per MOD instrument the note it is anchored at, the pitch
that note sounds and its sample's detune, read from the catalogue (`FmInstrument` / `PsgInstrument`
`synth_idx`, `root_semitone`); the PSG generator takes its rate and rendering pitch from the same
records.

### core/plan/synth_roots.py

**`resolve_synth_roots(song, config)`**: every rooted map entry's `synth_root` (the chip pitch its notes play
most often, at most an octave above `low`'s) and `synth_shift`, from a `walk_channel` over the song.  One pitch
per instrument where several entries share it.  Details: `docs/fm_synthesis.md` § synth_root.

### core/merge/

```
notes.py   NoteOn, channel_notes, pair_channels, layer and composite keys   (merge_survey, fold_csv)
model.py   MergePlan, Composite                                              <- notes
slots.py   fit_composites, same_shape_twins, stand_in, drop_composite        <- model, notes
pool.py    the fill pool, solo-note splicing                                 <- model, notes
plan.py    prepare_merged_config, build_merge_plan (_Planner)                <- all of the above
mix.py     mix_pcm_composites (_Mixer)                                       <- model
banks.py   pack_banks: the banked mixes in one slot each (9xx)                   <- mix, model, slots
build.py   MergedBuild: inside one conversion (core/convert/smps2mod.py)        <- banks, mix, model
```

The package exports what other modules import (`core.merge.MergePlan`, `core.merge.MergedBuild`, ...).

Folding SMPS channels onto one MOD channel for the Amiga build (`merge:` groups,
`convert.py --merged`). `prepare_merged_config` disables the followers, packs the remaining
channels onto MOD channels 0..n-1 and picks the output file; `build_merge_plan` lines every
follower's note-ons up with its primary's (`channel_notes`, `pair_channels` → `PairStats`) and
allocates one composite instrument per distinct (primary instrument, follower layers) key — two
FM voices as chip layers in the catalogue, anything else mixed from the finished samples by
`mix_pcm_composites`, the follower resampled by the period ratio of the two notes. The plan sits on
`config.merge_plan`, read by `walk_channel`; `refresh_ticks` rebuilds its tick map after the loop
bodies are extended. A follower note that starts while the primary is silent is spliced into
the primary's event stream as the follower's own note (`splice_solo_notes`; `walk_channel`
yields it with the follower's state), and instruments no note of the merged build plays are
dropped from the catalogue (`MergePlan.unused`). `tools/merge_survey.py` runs the same pairing
over every channel pair of a song. Full rules: `docs/pipeline.md` § Channel merging.

### core/convert/smps2mod.py

Conversion engine: `SmpsToModConverter` orchestrates one conversion; each step lives in its own
module and reports through one `Diagnostics` (core/diagnostics.py: warnings, de-duplicated, and
infos, which core/ui/report.py prints).  A record's kind is a `WarningKind` / `InfoKind` member
(`diag.warn(WarningKind.CLAMP_HIGH, channel=..., ...)`); every `WarningKind` has its line and fix in
core/ui/report.py's `_WARNINGS` (tests/test_diagnostics_units.py checks it).

```
SmpsToModConverter.convert()
  ├─ core/smps/song_prep.py            apply_global_tempo_div, extend_looping_channels: the song as played
  ├─ core/plan/timeline.py             Timeline: tempo segments, ticks per frame, BPM, tick → row / pattern, seconds
  ├─ core/convert/level_plan.py        LevelPlanner: the baked levels, the FM render levels
  ├─ core/plan/noise_derive.py         derive_noise_envelopes, derive_rate3_dividers
  ├─ core/convert/sustain_plan.py      SustainPlanner: auto sustain per instrument, sustain_short warnings
  ├─ core/merge/build.py               MergedBuild: composite volumes, pcm mixes and banks; the plan's report
  ├─ core/convert/channel_writer.py    ChannelWriter: one channel's cells (vibrato speed and depth from core/convert/vibrato.py)
  └─ core/convert/layout.py            ModLayout: leading rests, tempo commands, the loop's Bxx
```

#### `SmpsToModConverter.convert()` Flow

1. Set song name; `resolve_synth_roots` fills in every rooted entry's rendering pitch; `_plan_detune` (core/plan/detune.py) the detune variants
2. `prepare_song()`: re-time every channel for `smpsSetTempoDiv` (`apply_global_tempo_div()`) and extend short loop bodies (`extend_looping_channels()`); in the merged build, `_build_merge_plan()` (core/merge/) then decides the composite instruments while the ticks are final
3. Resolve `sustain_duration: auto` from the longest ring each instrument plays (`SustainPlanner._needs`)
4. Run the injected `SampleGenerators` (`generate_fm_samples()` from ym2612/, `generate_psg_samples()` from sn76489/) over the instrument catalogue (core/plan/instruments.py); load the DAC samples from disk; mix the merge plan's pcm composites
5. Set BPM (Fxx on pattern 0, channel 0) and speed (Fxx on pattern 0, channel 1)
6. Convert all channels via `_convert_all_channels()`, which first plans the baked levels
7. Write mid-song `smpsSetTempoMod` changes (`ModLayout.tempo_changes()`)

`convert()` then lays the MOD out: `apply_pattern_breaks`, `ModLayout.loop_point()` (so the `Bxx`
lands at its post-break position), trailing patterns trimmed, a merged build narrowed.

#### Public API

What the config tools (`merge_survey`, `fold_csv`, `config_to_chip_space`) see of the song, as
`convert()` sees it; nothing else on the converter is theirs to call:

| Member | What |
|---|---|
| `prepare_song()` | Step 2 alone: tempo re-timing, loops replayed, tempo segments collected; once |
| `level_baselines()` | `{"FM" \| "PSG": {instrument: dB}}` for the baked kinds |
| `pan_law_db` | Hard-pan attenuation (settings.yaml `fm_pan_law_db`) |
| `pattern_of_tick(tick)` / `last_pattern()` | The reference build's pattern, after its breaks |
| `tick_span_secs(start, end)` | MOD seconds between two ticks, across tempo changes |
| `sample_secs()` | Seconds a drum or noise sample sounds |

#### Channel Conversion

Every pass over a channel's events - `ChannelWriter`, the `LevelPlanner.levels` level pre-passes,
`SustainPlanner._needs`, `derive_noise_envelopes` and `derive_rate3_dividers` - is a `walk_channel`
(`core/plan/driver_state.py`): the same `DriverState`, and every pitched note's instrument and MOD note
from the same `resolve_note`, so they cannot disagree about what a note plays. `ChannelWriter`
keeps only the MOD-emission state (note fill, vibrato, cursor, `EDx`); its clamp / `map_gap`
warnings come from `_warn_resolution`.

| DriverState field | Updated by | Used for |
|-------------------|------------|----------|
| `tl` / `att` | `smpsAlterVol`, starting from the header volume | the note's level, hence `Cxx` |
| `hard_panned` | `smpsPan` | the -3 dB pan term in the FM level |
| `transpose` | `smpsChangeTransposition`, starting from the header pitch offset | pitch, and the `range_space: chip` lookup |
| `voice` | `smpsSetvoice` | `voice_map` / `channel_instrument_map` lookup |
| `psg_entry` / `psg_entries` / `psg_label` | `smpsPSGform` to `psg_map`, `smpsPSGvoice` to `psg_voice_map`, and the `smpsHeaderPSG` voice | PSG instrument, root anchoring, warning context |
| `instrument` | all of the above | the MOD instrument a note lands on |

`ChannelWriter` keeps only the state the driver knows nothing about:

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

### core/ui/cli.py

Shared Rich chrome for `convert.py`, `analyze.py` and `sonic2wav.py`: `cli_console()` (with the
Windows UTF-8 stdout fix, idempotent), `branding(console, product, version)`, `row_printer`,
`error_printer`, and the `LABEL_W` label column width.

### core/cbuild.py

**`CLibrary`** — a spec (`name`, `out_dir`, `sources`, `include`, `defines`, `gcc_libs`) plus
`get_lib_path()`, which rebuilds when a source is newer than the cached `.dll` / `.so` and
compiles with gcc or MSVC. `ym2612/build.py` and `sn76489/build.py` are each ~30 lines of spec
over it.

### convert.py

CLI entry point using `argparse`.

#### Arguments

| Argument | Short | Default | Description |
|----------|-------|---------|-------------|
| `config` | — | — | YAML config file (positional, required) |
| `--output` | `-o` | auto | Output .mod path |
| `--merged` | — | off | The merged build (`merge:` / `merge_patterns:`) |
| `--verbose` | `-v` | off | Also every composite, bank sound, loop extension, synthesis pitch; each sample's release rate and share of the song |

YAML config is the required positional argument. `--output` overrides `output_file` from YAML.

Output path defaults to `<input_basename>.mod` if not set in YAML.

Console chrome (branding panel, label column, error printer) comes from `core/ui/cli.py`, shared with
`analyze.py` and `sonic2wav.py`.

The converter reports through two public lists, `SmpsToModConverter.warnings` and `.infos`, each
holding dicts with a `type` key, plus `sample_sources()` (what each slot holds: kind, source, the
rate it was rendered at, the release rate).  `core/ui/report.py` turns them into the report: a header
(source, tempo, settings, output), **Checks** (tempo, pitch, samples, merge, levels, patterns: a tick
or each warning with its fix), **Channels** (or, merged, the MOD **Columns**), **Samples** (every
slot against the sample limit, its loop and the notes that play it, read back from the written file
by `core/mod/sample_audit.py`), **Merge** (merged build: each fold's composites, folded / solo / lost /
cut notes) and, with `--verbose`, **Details**.  Warnings go through `_WARNINGS`, a `{type:
function}` table returning `(check, headline, fix)` - a new warning type is one function and one
entry; an unrecognised type is shown raw under `other`.

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
synthesizes each, peak-normalises and quantises it (dithered, through `core.audio.pcm.to_int8`),
and returns a `{inst_num: (pcm_bytes, sample_rate_hz)}` dict ready for
`ModFile` insertion.

### validate.py

Standalone smoke test: `python sn76489/validate.py` renders a C3 tone and white noise,
writing `output/psg_tone_test.raw` and `output/psg_noise_test.raw` (16-bit for Audacity).
