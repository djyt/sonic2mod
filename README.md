# SONIC2MOD

Convert Sega Mega Drive game music (Sonic 1, Streets of Rage, OutRun and more) to ProTracker MOD format.

> reassembler 2026 | https://youtube.com/@reassembler68k | https://reassembler.blogspot.com

## Features

- Input: the game's ROM, or SMPS assembly (Sonic only)
- Instruments rendered on cycle-accurate YM2612 (Nuked-OPN2) and SN76489 emulators
- Driver-accurate playback: tempo, note fill, modulation, detune, transposition, loops
- Effects mapped to ProTracker: vibrato, slides, volume, note delays, pattern breaks
- 4-Channel Amiga builds: channels reduced to 4, with composite instruments and lofi variants
- Verification against VGZ rips: per-note pitch, level, timing and balance audits
- Sound effects rendered to WAV, or 8-bit samples with a manifest for the Amiga


## Games Supported

| Game | Driver | Configs |
|------|--------|---------|
| Sonic The Hedgehog | SMPS 68k | `configs/sonic_1/` |
| Michael Jackson's Moonwalker | SMPS 68k (Type 1a) | `configs/moonwalker/` |
| Golden Axe | SMPS Z80 (Type 0 FM) | `configs/golden_axe/` |
| Space Harrier II | SMPS Z80 (early) | `configs/space_harrier_2/` |
| OutRun | SMPS Z80 (Timer A) | `configs/outrun/` |
| Streets of Rage | SMPS 68k (MUCOM-style) | `configs/streets_of_rage/` |
| Streets of Rage 2 | Mucom-MD | `configs/streets_of_rage_2/` |
| Time Trax | Tim Follin's Z80 driver | `configs/time_trax/` |


## Requirements

- Python 3.11+
- GCC or MSVC on PATH *(if on Linux or Mac to build the third-party libraries)*

### Optional

Aside from the ROMs, most of the following are only required for development work.

| Requirement | Where | Used for |
|-------------|-------|----------|
| Game ROMs | `input/roms/` | Every supported game aside from Sonic's asm song data |
| Sonic 1 driver disassembly | `reference/smps_drivers/sonic_1/` | `sonic2wav --all` (its SFX asm), or use `sonic2wav --rom` |
| VGZ rips | `reference/vgz/<game>/` | The `tools/vgm/` audits against the Mega Drive |
| VGMPlay 0.51.x | `reference/vgz/vgmplay/` | `vgm_compare.py`: renders the rips |
| ffmpeg with libopenmpt | PATH | `vgm_compare.py`: renders the MODs |
| `pip install -e .[dev]` | — | Lint, type checks, tests |
| `capstone`, `z80dis` (in `[dev]`) | — | `tools/rom/disasm.py`: a driver's 68k / Z80 code |


## Installation

```bash
git clone https://github.com/djyt/sonic2mod
cd sonic2mod
pip install -e .
```

This registers three CLI commands: `sonic2mod` (convert), `sonic2mod-analyze` (analyse) and
`sonic2wav` (render the sound effects). All three take `--version`.


## Quick Start

There are two main components to this package:

1/ A **converter** to convert SMPS files to MOD, including FM and PSG synthesis.

2/ An **analyzer** to parse Sonic's SMPS files. *(Only required if you're planning to make your own configs from scratch - if you just want to convert the existing Sonic tracks you can ignore this).*

Once installed, run the following commands from the repo root:

**Convert a song:**

```bash
sonic2mod configs/sonic_1/02_green_hill_zone.yaml
```

Or using Python directly:

```bash
python convert.py configs/sonic_1/02_green_hill_zone.yaml
```

Output `.mod` files are written to `output/sonic_1/` (or whichever game you converted) and can be opened in [Fast Tracker 2 Clone](https://16-bits.org/ft2.php).

That's it - nice and easy. The complexity comes if you want to extend the tool further really. 


## How It Works

```
  song (asm or ROM)
        │
        ▼
  ┌───────────┐   one per sound driver: reads the song, plays it as the game does
  │  driver   │   core/drivers/
  └─────┬─────┘
        │
        ▼
  ┌───────────┐   configs/settings.yaml   global: synthesis, sample rates, sizes
  │ converter │ ◄
  └─────┬─────┘   configs/<game>/*.yaml   per song: instruments, ranges, levels
        │
        ├──► song.mod           every channel of the original
        ├──► song_merged.mod    channels folded to 4 for the Amiga (merge groups)
        └──► song_<variant>     a version differing in a few ways (e.g. lofi: smaller samples)
```

- **Driver:** each game's sound driver is read as data: its song format, tables and timing.
- **Settings:** `settings.yaml` holds what applies to every song.
- **Song config:** names the song and maps its voices to instruments. A minimal config names only
  the song; the rest is derived.
- **Merged build:** folds channels into composite instruments so the song fits four channels.
- **Variants:** a `variants:` block in a config overrides a few keys for an alternative build.


## Sonic Music

Pre-configured conversions are in `configs/sonic_1/`

| Config | Song | 4-channel |
|--------|------|-----------|
| `01_title_screen.yaml` | Title Screen | Yes |
| `02_green_hill_zone.yaml` | Green Hill Zone | Yes |
| `03_marble_zone.yaml` | Marble Zone |  |
| `04_spring_yard_zone.yaml` | Spring Yard Zone |  |
| `05_lab_zone.yaml` | Labyrinth Zone |  |
| `06_star_light_zone.yaml` | Star Light Zone |  |
| `07_scrap_brain_zone.yaml` | Scrap Brain Zone |  |
| `08_special_stage.yaml` | Special Stage |  |
| `09_robotnik.yaml` | Boss (Robotnik) | Yes |
| `10_final_zone.yaml` | Final Zone |  |
| `11_stage_clear.yaml` | Stage Clear | Yes |
| `12_ending_theme.yaml` | Ending Theme |  |
| `13_credits.yaml` | Credits |  |
| `14_invincibility.yaml` | Invincibility | Yes |
| `15_1up.yaml` | 1-Up / Extra Life | Yes |
| `16_chaos_emerald.yaml` | Chaos Emerald |  |
| `17_drowning.yaml` | Drowning |  |
| `18_continue_screen.yaml` | Continue Screen | Yes |
| `19_game_over.yaml` | Game Over | Yes |


## Render sound effects to WAV

Sonic 1's 49 sound effects skip the MOD pipeline. `sonic2wav` runs a frame-accurate copy of the Sonic 1 sound driver on the same chip emulators,
as `configs/sfx.yaml` says: output folder, format, bit depth, rate, level and more.

```bash
sonic2wav --all                    # 16-bit stereo 44.1 kHz WAV → output/sonic_1/sfx/
sonic2wav --all --dry-run          # render and report, write nothing
sonic2wav "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"   # one effect
```

A variant in `sfx.yaml` lays its keys over the defaults. `amiga` writes signed 8-bit mono `.raw` files
on ProTracker's period table, with a `manifest.yaml` giving each sample's note, volume and repeat:

```bash
sonic2wav --all --variant amiga    # → output/sonic_1/sfx/amiga/
```

Details, the 8-bit chain and the deviations from stock hardware: `docs/guide/sound_effects.md`.


## Make a config for a new song

Example: Create a config file from the Green Hill Zone assembly:
```bash
sonic2mod-analyze "input/sonic_1/Mus81 - GHZ.asm"
```

Save the generated config to a file:

```bash
sonic2mod-analyze "input/sonic_1/Mus81 - GHZ.asm" --write configs/sonic_1/my_ghz.yaml
```

With an existing config to show coverage:

```bash
sonic2mod-analyze "input/sonic_1/Mus81 - GHZ.asm" --config configs/sonic_1/02_green_hill_zone.yaml
```

The analyzer isn't perfect, makes mistakes and bad decisions. Like us all. Expect to hand-tweak its output in certain cases to get the best possible results. 


## How close is it?

The test harness can check the final MOD against a VGM/VGZ recording of the Mega Drive output.
Notes, timing, chip frequency register writes and perform a pitch and volume audit. 

As of the current configs Sonic has **7793 of 7802 notes correct**, none missing. 
The 9 notes that differ are in Spring Yard and the Credits, and are explained in `docs/audits/`.


## Limitations

- **One sample per range.** A range is synthesised at a single pitch (`synth_root`) and the Amiga
  resamples it for the rest, so anything time-based in the patch scales with playback rate: the
  envelope runs faster on high notes, and detuned-carrier beating changes speed with pitch. A
  minimal config caps each range at nine semitones (`samples.max_window`); in a full config, split
  wider ranges into two entries with their own `root`.
- **Noise samples have one LFSR rate.** Pitched noise (Marble Zone) follows the melody by MOD
  playback speed rather than by re-clocking the LFSR.
- **A MOD BPM is a whole number**, so a song's tempo can land slightly off. `convert.py` prints the
  rounding error and the `target_speed` that would reduce it.


## FM/PSG Synthesis

Cycle-accurate YM2612 (FM) and SN76489 (PSG) synthesis is enabled by default. The emulators' C sources are in `3rdparty/`. With GCC or MSVC in your path they're compiled into `build/` the first time they're needed; without one, Windows uses the pre-compiled DLLs in `prebuilt/`. On Linux or a Mac you'll need GCC installed and in your path.

I chose to leave both of these emulators as C for performance, ease of future upgrading, and the high chance of introducing bugs if I was to convert them to Python!

To disable synthesis and use pre-rendered `.raw` sample files that you have provided instead (read from the config's `samples_dir`, e.g. `input/sonic_1/samples/`, which holds Sonic 1's DAC drums), set in `configs/settings.yaml`:

```yaml
fm_synthesis:
  enabled: false
psg_synthesis:
  enabled: false
```

Smoke tests (generate a basic sample):

```bash
python tools/chips/validate_ym2612.py
python tools/chips/validate_sn76489.py
```


## Documentation

Where to read next depends on what you want to do:

| You are… | Read |
|----------|------|
| **A listener**: you want the MODs | Quick Start above; `docs/guide/amiga_builds.md` for 4-channel versions |
| **An arranger**: you are tuning a song's sound | `docs/guide/song_configs.md`, then `docs/conversion/overview.md` |
| **Checking accuracy** against the hardware | `docs/guide/verifying.md` |
| **Adding a game** | `docs/drivers/README.md` |
| **Changing the code** | `docs/developer/architecture.md` |

Overview: `docs/README.md`.  An unfamiliar term: `docs/glossary.md`.

There is also a CLAUDE.md file, so you can experiment with adding functionality (or simply breaking everything) with an AI coding agent. I've found Claude Code to struggle with low-level assembly, but AI is evolving so fast that may have all changed by the time you read this! It's very good at some of the boring Python maintenance, and I used it for the above documentation. It's me writing right now though! :-)

I'm not particularly looking for a load of chaotic AI driven push requests at this moment in time, but I'm happy to get robust and meaningful changes merged in. Or feel free to fork the codebase and do it your own way. Don't let me slow down your dreams!


## History

I originally coded a similar tool to facilitate the translation of OutRun's music to the Amiga. However, it was hacky, esoteric and probably unusable by anyone other than myself. As such, this is an evolution of that process. I was able to get results I was happy sharing within weeks, partially thanks to this pre-existing codebase and also using AI to handle some of the boring bits involved with releasing software!


## Licensing

This is licensed under the GNU LESSER GENERAL PUBLIC LICENSE. This is the same license as the Nuked-OPN2 core, which is included in the codebase.


## Thanks & Acknowledgements

- The [Sonic Retro Team](https://info.sonicretro.org) for their excellent decompilation of Sonic The Hedgehog
- [Nuked-OPN2](https://github.com/nukeykt/Nuked-OPN2) for the YM2612 core
- [Maxim](https://www.smspower.org/maxim/) for his SN76489 core
- [8bitbubsy](https://16-bits.org/) for FT2 Clone
