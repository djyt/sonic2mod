# settings.yaml

`settings.yaml` holds what every song shares: how samples are synthesised on the emulated chips, sample size
and quality limits, and MOD-wide choices (legato, the target tracker, volume normalising).  This page lists
every key with its code default and the value `configs/settings.yaml` ships.  Per-song keys are in
[song_configs.md](song_configs.md).

## Where it is read from

`--settings PATH`, else `settings.yaml` beside the song config, else `configs/settings.yaml`.  A value it
states is used by every CLI and tool; the code's constants are only the fallback.  With no file at all the
code defaults apply: synthesis **off**, so FM and PSG slots load `sample_list` files from disk.

```bash
python convert.py configs/sonic_1/01_title_screen.yaml --settings tests/settings.yaml
```

A key the loader does not know is an error, in every section.  Regression runs use `tests/settings.yaml`, which
must state the same keys as the live file (the runner exits 2 otherwise): add a new key to both.
`sonic2wav.py` reads the chip clocks, `amiga_clock`, `psg_synthesis.oversample` and `samples.resample_taps`
from `configs/settings.yaml` too ([sound_effects.md](sound_effects.md)).

A song config can override three `samples:` keys for itself: `loop_drift_db`, `treble_shelf_db`, `max_window`
([song_configs.md](song_configs.md)).  The levels, legato and sample keys are explained on the
[conversion pages](../conversion/overview.md); this page only lists them.

```yaml
# configs/settings.yaml (excerpt)
fm_synthesis:
  enabled: true
  sustain_duration: auto
samples:
  max_sample_kb: 64
  sustain_loops: all
legato: retrigger
merged_player: pt2
```

## Top level

| Key | Code default | Shipped | What it does |
|---|---|---|---|
| `amiga_clock` | `3546895` | same | Paula's clock (PAL): a note's rate is this / its period.  C-2 (period 428) = 8287 Hz |
| `fm_volume_scaling` | `baked` | `baked` | `baked`: an instrument's commonest level is its `sample_list` volume, `Cxx` elsewhere ([glossary](../glossary.md#baked-level)).  `absolute` (or `true`): header TL as an absolute volume, `Cxx` on every FM note.  `off` (or `false`): one `smpsAlterVol` step = one linear volume unit (inaccurate).  [levels.md](../conversion/levels.md) |
| `fm_pan_law_db` | `3.0` | `3` | `baked` only: dB a hard-panned FM note counts quieter than a centred one (a centred YM2612 channel drives both speakers, an Amiga channel one).  0 = off |
| `psg_volume_scaling` | `baked` | `baked` | The PSG's: `baked` (2 dB a step) or `absolute` (`Cxx` on nearly every PSG note) |
| `legato` | `retrigger` | `retrigger` | How an `smpsNoAttack` note is written: `retrigger` (a plain note-on), `strict` (`3FF` where the sounding sample can reach it), `loose` (`3FF` always).  [effects.md](../conversion/effects.md) |
| `player` | `ft2` | `ft2` | The tracker `4xy` vibrato depths are fitted to: `ft2` (FT2 clone) or `pt2` (ProTracker 2, an Amiga).  At the same depth PT2's swing peaks ¾ of a period below FT2's (`2y − 1` against `2y − ¼` periods: [effects.md](../conversion/effects.md#rules)) |
| `merged_player` | `player`'s | `pt2` | The same for a `--merged` build (the Amiga build, played by a ProTracker 2 replayer).  `convert.py --player` overrides both |
| `normalise_volumes` | `reference` | `reference` | `reference`: every volume of the reference build (sample volumes and `Cxx`) raised by one gain until its loudest note plays at 64.  `off`: the `sample_list`'s as they are.  The merged build never.  [levels.md](../conversion/levels.md) |
| `end_trigger` | off | `1` | A song that stops ends with `E8x` (x = this, 1–15) on its last row, for a replayer to report the end (ptplayer's `_mt_E8Trigger`).  `off` or `0`: none.  [song_structure.md](../conversion/song_structure.md) |

## `fm_synthesis:`

| Key | Code default | Shipped | What it does |
|---|---|---|---|
| `enabled` | `false` | `true` | Render FM instruments on the emulated YM2612 (else load `sample_list` files) |
| `mode` | `ym2612` | `ym2612` | Chip variant: `ym2612` (the Mega Drive's) or `ym3438` |
| `clock_rate` | `7670454` | same | YM2612 clock, NTSC (Hz) |
| `sustain_duration` | `1.5` | `auto` | Seconds a note is held before key-off; `auto`: each instrument's longest ring, capped at 10 s.  [fm.md](../synthesis/fm.md) |
| `release_padding` | `0.5` | `0.5` | Seconds rendered after key-off |
| `detune_variants` | `true` | `true` | An `smpsAlterNote` / `smpsDetune` note plays its instrument rendered at that FNUM offset, in a free slot ([detune.md](../conversion/detune.md)); `false`: the plain sample |
| `drum_psg_db` | `-5.7` | `-5.7` | An FM drum's PSG part (Space Harrier II's noise drums) in dB against its FM part; 0 = a full PSG channel level with one TL 0 carrier.  Measured on the rips (VGMPlay mixes the PSG at half the YM2612's volume): −5.2 to −6.9 dB, median −5.7 |
| `threads` | `normal` | `normal` | Render threads: `normal` (cores − 1), `max` (every core) or a number.  Output does not depend on it |

## `psg_synthesis:`

| Key | Code default | Shipped | What it does |
|---|---|---|---|
| `enabled` | `false` | `true` | Render PSG instruments on the emulated SN76489 |
| `clock_rate` | `3579545` | same | SN76489 clock, NTSC (Hz) |
| `sustain_duration` | `1.0` | `auto` | As for FM |
| `release_padding` | `0.2` | `0.2` | As for FM |
| `oversample` | `8` | `8` | Render at this multiple of the sample's rate, then resample down.  1: the core's box filter (fast, aliases); 8: flat.  [psg.md](../synthesis/psg.md) |

## `samples:`

Shared by both chips' samples.

| Key | Code default | Shipped | What it does |
|---|---|---|---|
| `max_sample_kb` | `128` | `64` | Largest sample, 1–128: 128 = the format's 131070 bytes, 64 = ProTracker's 65534 |
| `sustain_loops` | `merged` | `all` | Which builds loop settled samples and end FM notes with release slides: `off`, `merged`, `all`.  [samples.md](../conversion/samples.md) |
| `loop_drift_db` | `1.0` | `1` | dB above the settled level a loop may freeze: 1 loops voices that truly settle; 3–12 loops slowly decaying ones too (smaller files).  Song, entry and group keys override it |
| `dither` | `shaped` | `shaped` | 8-bit quantisation: `shaped` (noise pushed up the band, under a bright sound's treble), `flat` (even; less hiss on a mellow sound), `off` (plain rounding; a quiet tail steps) |
| `dc_block` | `false` | `true` | 5 Hz high-pass on every render, as the Mega Drive's AC-coupled output does: no click on note-on and cut.  Disk samples are left alone |
| `pt_zero_bytes` | `true` | `true` | Zero a one-shot sample's first word: ProTracker replays it after the sample ends (else a buzz or DC step) |
| `compact_slots` | `merged` | `merged` | Renumber the used slots from 1 without gaps: `off`, `merged`, `all` (`all`: slots no longer match the config's numbers) |
| `trim_tails` | `off` | `merged` | Cut each one-shot sample after the last byte any note can be heard to, found by replaying the MOD as ProTracker does: `off`, `merged`, `all`.  Lossless; disk samples stay whole.  [samples.md](../conversion/samples.md) |
| `names` | `source` | `source` | Sample names: `source` (what plays it: `F1/3/4/5 $05 C4-B5`, `D dKick B-2`) or `file` (the `sample_list` file name).  An entry's `name:` wins |
| `treble_shelf_db` | `0` | `0` | Brightness shelf on every render above `treble_shelf_hz`, before loops and quantising; 0 = off (as the hardware) |
| `treble_shelf_hz` | `2500` | `2500` | The shelf's corner, at the sample's own pitch |
| `resample_taps` | `32` | `32` | Resampler width (even): 32 is flat to 85 % of Nyquist, aliases 78 dB down; 16 is ~2/3 the time, −0.6 dB at 85 %, 73 dB |
| `render_cache` | off | `output/cache` | Directory (relative to the project root) keeping chip renders under a hash of their inputs and the emulator code; output is identical either way.  `off` disables |
| `root_harmonics` | `8` | `8` | Minimal configs: harmonics a window's lowest note keeps below Nyquist; 0 = every window at E1 (smallest), 16 = mid voices move up too.  [song_configs.md](song_configs.md#windows) |
| `top_note` | `A3` | `A3` | Minimal configs: a window's highest MOD note.  A#3 (period 120) and B3 (113) pass Paula's period-124 limit and sound bad on an Amiga |
| `max_window` | `0` | `9` | Minimal configs: widest window in semitones; 0 = no cap.  9: an envelope runs 1.7× too fast at worst; 12: 2× |
| `drum_root` | `C3` | `C3` | Minimal configs: the MOD note an FM drum (Golden Axe's, Space Harrier II's: rendered whole) is rendered for and played at: C2 ≈ 8.3 kHz, C3 ≈ 16.6 kHz, A3 ≈ 27.9 kHz |

`off` / `on` may be written bare (YAML reads them as booleans; the loader takes either).
