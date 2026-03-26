# sonic2mod

Converts Sonic 1 SMPS assembly music files to Amiga MOD format.

## Requirements

- Python 3.11+
- GCC or MSVC on PATH *(only needed if recompiling the synthesis DLLs — pre-compiled Windows binaries are included)*

## Installation

```bash
git clone https://github.com/your-username/sonic2mod
cd sonic2mod
pip install .
```

For a development install (edits to source take effect immediately):

```bash
pip install -e .
```

This registers two CLI commands: `sonic2mod` and `sonic2mod-analyze`.

## Usage

Run all commands from the repo root.

**Convert a song:**

```bash
sonic2mod configs/02_green_hill_zone.yaml
```

Or using Python directly:

```bash
python convert.py configs/02_green_hill_zone.yaml
```

Override the output path:

```bash
sonic2mod configs/02_green_hill_zone.yaml --output output/ghz.mod
```

**Analyse a song (no config needed):**

```bash
sonic2mod-analyze "input/Mus81 - GHZ.asm"
```

With a config to show coverage:

```bash
sonic2mod-analyze "input/Mus81 - GHZ.asm" --config configs/02_green_hill_zone.yaml
```

Output `.mod` files are written to `output/` and can be opened in [OpenMPT](https://openmpt.org/) or [MilkyTracker](https://milkytracker.org/).

## Songs

Pre-configured conversions are in `configs/`:

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
| `17_drowing.yaml` | Drowning |
| `18_continue_screen.yaml` | Continue Screen |
| `19_game_over.yaml` | Game Over |

## FM/PSG Synthesis

Cycle-accurate YM2612 (FM) and SN76489 (PSG) synthesis is enabled by default. Samples are generated automatically on first run using the bundled C emulators, which requires GCC or MSVC on PATH.

To disable (uses pre-rendered `.raw` sample files from `samples/` instead), set in `configs/settings.yaml`:

```yaml
fm_synthesis:
  enabled: false
psg_synthesis:
  enabled: false
```

Smoke tests:

```bash
python ym2612/validate.py
python sn76489/validate.py
```

## Documentation

| Document | Contents |
|----------|----------|
| `docs/pipeline.md` | Conversion pipeline — SMPS→MOD effect mapping, BPM derivation |
| `docs/smps_driver.md` | Sonic 1 driver reference — all coord flag bytes, timing |
| `docs/yaml_config.md` | Full YAML config schema |
| `docs/fm_synthesis.md` | YM2612 synthesis pipeline |
| `docs/psg_synthesis.md` | SN76489 PSG synthesis pipeline |
