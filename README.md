# SONIC2MOD

Convert Sonic The Hedgehog 1 SMPS assembly music files from the Sega Megadrive to MOD format.

> reassembler 2026 | https://youtube.com/@reassembler68k | https://reassembler.blogspot.com

## Requirements

- Python 3.11+
- GCC or MSVC on PATH *(only needed if recompiling the synthesis DLLs — pre-compiled Windows binaries are included)*


## Installation

```bash
git clone https://github.com/djyt/sonic2mod
cd sonic2mod
pip install .
```

For a development install (edits to source take effect immediately):

```bash
pip install -e .
```

This registers two CLI commands: `sonic2mod` and `sonic2mod-analyze`.


## Quick Start

There are two main components to this package:

1/ A **converter** to convert SMPS files to MOD, including FM and PSG synthesis.

2/ An **analyzer** to parse Sonic's SMPS files. *(Only required if you're planning to make your own configs from scratch - if you just want to convert the existing Sonic tracks you can ignore this).*

Once installed, run the following commands from the repo root:

**Convert a song:**

```bash
sonic2mod configs/02_green_hill_zone.yaml
```

Or using Python directly:

```bash
python convert.py configs/02_green_hill_zone.yaml
```

Output `.mod` files are written to `output/` and can be opened in [Fast Tracker 2 Clone](https://16-bits.org/ft2.php).

That's it - nice and easy. The complexity comes if you want to extend the tool further really. 


## Sonic Music

Pre-configured conversions are in `configs/`

| Config | Song |
|--------|------|
| `01_title_screen.yaml` | Title Screen |
| `02_green_hill_zone.yaml` | Green Hill Zone |
| `03_marble_zone.yaml` | Marble Zone |
| `04_spring_yard_zone.yaml` | Spring Yard Zone |
| `05_lab_zone.yaml` | Labyrinth Zone |
| `06_star_light_zone.yaml` | Star Light Zone |
| `07_scrap_brain_zone.yaml` | Scrap Brain Zone |
| `08_special_stage.yaml` | Special Stage |
| `09_robotnik.yaml` | Boss (Robotnik) |
| `10_final_zone.yaml` | Final Zone |
| `11_stage_clear.yaml` | Stage Clear |
| `12_ending_theme.yaml` | Ending Theme |
| `14_invincibility.yaml` | Invincibility |
| `15_1up.yaml` | 1-Up / Extra Life |
| `16_chaos_emerald.yaml` | Chaos Emerald |
| `17_drowning.yaml` | Drowning |
| `18_continue_screen.yaml` | Continue Screen |
| `19_game_over.yaml` | Game Over |


## Analyze an SMPS file (create a new config that doesn't already exist)

Example: Create a config file from the Green Hill Zone assembly:
```bash
sonic2mod-analyze "input/Mus81 - GHZ.asm"
```

With an existing config to show coverage:

```bash
sonic2mod-analyze "input/Mus81 - GHZ.asm" --config configs/02_green_hill_zone.yaml
```

The analyzer isn't perfect, makes mistakes and bad decisions. Like us all. Expect to hand-tweak its output in certain cases to get the best possible results. 


## Render sound effects to WAV

Sonic 1's 49 sound effects are a different problem to its music: they're 60 Hz tick-driven, mostly under a second, and built almost entirely from per-tick pitch sweeps and volume ramps that a tracker row grid would flatten. So they don't go through the MOD pipeline at all.

`sonic2wav` instead runs a tick-accurate reimplementation of the Sonic 1 sound driver against the same YM2612 and SN76489 emulators, producing a continuous timeline:

```bash
sonic2wav --all
```

Or using Python directly:

```bash
python sonic2wav.py --all
```

Output `.wav` files are written to `output/sfx/` as 16-bit stereo 44.1 kHz — stereo because hard panning is real design intent in Sonic 1 (`B5_Ring.wav` is right-only, and `CE_Ring_Left_Speaker.wav` is its left-channel twin).

Render a single effect, or check what would be produced without writing anything:

```bash
python sonic2wav.py "sonic_1/sfx/SndB5 - Ring.asm"
python sonic2wav.py --all --dry-run
```

Useful options: `--rate native` writes at the chip's own 53267 Hz and skips resampling, `--psg-gain` sets the PSG level against the FM, `--no-normalize` writes raw chip levels. By default a single global gain is applied across all 49 files, which keeps the relative loudness the composers intended rather than making everything equally loud.

### 8-bit samples for the Amiga

```bash
python sonic2wav.py --all --8bit
```

Writes signed 8-bit mono `.raw` files to `output/sfx8/` alongside a `manifest.yaml` giving each sample's rate, the note to trigger it at, the suggested MOD volume and its repeat points.

Eight bits needs roughly the opposite treatment to the 16-bit set. Each sample is DC-corrected, resampled once straight from the chip rate, peak-normalised, and dithered with noise shaping — then the volume column restores the composed balance. Normalising per sample rather than globally is worth a median 1.5 bits, and 3.1 bits on the quietest effect.

Rates are chosen per effect from ProTracker's own period table, so each sample plays at true pitch with finetune 0. `--max-rate 16574` targets an A500 (whose fixed ~4.4 kHz filter makes more largely academic) and roughly halves the total; `--flat-rate N` forces a single rate for everything.

See `docs/sfx_rendering.md` for the driver details, the 8-bit chain, and the handful of documented deviations from stock hardware.


## FM/PSG Synthesis

Cycle-accurate YM2612 (FM) and SN76489 (PSG) synthesis is enabled by default. These have been pre-compiled for Windows and included as a DLL file. However, if you're using Linux or a Mac you'll need GCC or MSVC installed and in your path. 

I chose to leave both of these emulators as C for performance, ease of future upgrading, and the high chance of introducing bugs if I was to convert them to Python!

To disable synthesis and use pre-rendered `.raw` sample files from `samples/` that you have provided instead, set in `configs/settings.yaml`:

```yaml
fm_synthesis:
  enabled: false
psg_synthesis:
  enabled: false
```

Smoke tests (generate a basic sample):

```bash
python ym2612/validate.py
python sn76489/validate.py
```


## Documentation

| Document | Contents |
|----------|----------|
| `docs/yaml_config.md` | Full YAML config schema |
| `docs/pipeline.md` | Conversion pipeline — SMPS→MOD effect mapping, BPM derivation |
| `docs/smps_driver.md` | Megadrive Sonic 1 driver reference — all coord flag bytes, timing |
| `docs/fm_synthesis.md` | YM2612 synthesis pipeline |
| `docs/psg_synthesis.md` | SN76489 PSG synthesis pipeline |

There is also a CLAUDE.md file, so you can experiment with adding functionality (or simply breaking everything) with an AI coding agent. I've found Claude Code to struggle with low-level assembly, but AI is evolving so fast that may have all changed by the time you read this! It's very good at some of the boring Python maintenance, and I used it for the above documentation. It's me writing right now though! :-)

I'm not particularly looking for a load of chaotic AI driven push requests at this moment in time, but I'm happy to get robust and meaningful changes merged in. Or feel free to fork the codebase and do it your own way. Don't let me slow down your dreams!


## Limitations

- Mid-track tempo changes are currently ignored. This impacts *Drowning*, which should speed up as it progresses.
- The Credits music is not configured for translation. It's effectively a Megamix of all the existing Sonic music, and I suspect I'll handle that in a different way in the final port of Sonic to the Amiga.
- Synthesis is performed at the lowest note of the specified range in the current config files. It would be sensible to do this mid-range to minimise timbre changes to the final output. 


## Future Improvements

- Support other versions and forks of SMPS
- Binary input option
- Tools and helpers to combine samples and channels to target 4 channel MODs


## History

I originally coded a similar tool to facilitate the translation of OutRun's music to the Amiga. However, it was hacky, esoteric and probably unusable by anyone other than myself. As such, this is an evolution of that process. I was able to get results I was happy sharing within weeks, partially thanks to this pre-existing codebase and also using AI to handle some of the boring bits involved with releasing software!


## Licensing

This is licensed under the GNU LESSER GENERAL PUBLIC LICENSE. This is the same license as the Nuked-OPN2 core, which is included in the codebase.


## Thanks & Acknowledgements

- The [Sonic Retro Team](https://info.sonicretro.org) for their excellent decompilation of Sonic The Hedgehog
- [Nuked-OPN2](https://github.com/nukeykt/Nuked-OPN2) for the YM2612 core
- [Maxim](https://www.smspower.org/maxim/) for his SN76489 core
- [8bitbubsy](https://16-bits.org/) for FT2 Clone
