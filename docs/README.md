# sonic2mod documentation

sonic2mod turns Mega Drive game music into ProTracker MODs.  It reads a song the way the game's sound driver
plays it, renders each instrument on emulated Mega Drive sound chips, and writes the notes and effects into
MOD patterns.  These pages explain how to use it, how it works, and how to change it.

```
  song (asm, ROM or VGZ rip)
        │
        ▼
  ┌───────────┐  drivers/       reads the song as the game's driver plays it
  │  driver   │
  └─────┬─────┘
        ▼
  ┌───────────┐  conversion/    timing, notes, effects, levels, loops → MOD patterns
  │ converter │  synthesis/     each instrument rendered on a YM2612 / SN76489 emulator → samples
  └─────┬─────┘
        ├──► song.mod           every channel of the original      (the reference build)
        ├──► song_merged.mod    folded to 4 channels for the Amiga (guide/amiga_builds.md)
        └──► song_<variant>.mod a few keys changed, e.g. lofi       (guide/song_configs.md#variants)
```

New here?  Convert a song (`python convert.py configs/sonic_1/02_green_hill_zone.yaml`), open the MOD in
[FT2 Clone](https://16-bits.org/ft2.php), then read [Song configs](guide/song_configs.md).
Unfamiliar word?  See the [glossary](glossary.md).


## Where to start

| I want to… | Read |
|------------|------|
| Convert a song, or tune its config | [guide/song_configs.md](guide/song_configs.md) |
| Change a setting shared by every song | [guide/settings.md](guide/settings.md) |
| Make a 4-channel Amiga version | [guide/amiga_builds.md](guide/amiga_builds.md) |
| Check a MOD against the real hardware's output | [guide/verifying.md](guide/verifying.md) |
| Render Sonic 1's sound effects to WAV / Amiga samples | [guide/sound_effects.md](guide/sound_effects.md) |
| Understand why the MOD looks the way it does | [conversion/overview.md](conversion/overview.md) |
| Add a game, or learn how a game's driver works | [drivers/README.md](drivers/README.md) |
| Change the code | [developer/architecture.md](developer/architecture.md) |


## All pages

### Guide: using the tool

| Page | Contents |
|------|----------|
| [song_configs.md](guide/song_configs.md) | Running a config; minimal configs; building a full one; every song-config key; the merge-key cheat sheet; variants |
| [settings.md](guide/settings.md) | Every `settings.yaml` key: code default and shipped value |
| [amiga_builds.md](guide/amiga_builds.md) | The merged build step by step, and its rules: groups, composites, the fill pool, banks, slots |
| [verifying.md](guide/verifying.md) | Checking a conversion against a VGZ rip: which tool answers which question, setup, each tool |
| [sound_effects.md](guide/sound_effects.md) | `sonic2wav`: `configs/sfx.yaml`, mix levels, the 8-bit Amiga export |

### Conversion: how a song becomes a MOD

| Page | Contents |
|------|----------|
| [overview.md](conversion/overview.md) | The conversion at a glance; MOD limits |
| [timing.md](conversion/timing.md) | Ticks, rows, patterns, BPM; frames versus ticks; tempo changes |
| [notes.md](conversion/notes.md) | Note range; `voice_map` routing; `root`; `range_space` |
| [effects.md](conversion/effects.md) | Driver commands → MOD effects; priority; `EDx`; note fill; vibrato; legato |
| [levels.md](conversion/levels.md) | FM and PSG volumes; normalised volumes |
| [song_structure.md](conversion/song_structure.md) | Start, loop and end; pattern breaks |
| [detune.md](conversion/detune.md) | Detune variants; the hardware LFO |
| [samples.md](conversion/samples.md) | Sample length; sustain loops; release slides |

### Synthesis: rendering the instruments

| Page | Contents |
|------|----------|
| [fm.md](synthesis/fm.md) | YM2612: what is rendered, at which pitch and level, how long; the emulator; troubleshooting |
| [psg.md](synthesis/psg.md) | SN76489: tones, noise, envelopes; the emulator; troubleshooting |

### Drivers: the games' sound drivers

| Page | Contents |
|------|----------|
| [README.md](drivers/README.md) | Supported games; what every driver shares; adding a driver |
| [sonic_1.md](drivers/sonic_1.md) | Sonic 1's SMPS driver: coordination flags, timing, voices, DAC, PSG |
| [smps_asm.md](drivers/smps_asm.md) | SMPS assembly syntax and how it is parsed |
| [moonwalker.md](drivers/moonwalker.md) · [golden_axe.md](drivers/golden_axe.md) · [streets_of_rage.md](drivers/streets_of_rage.md) · [space_harrier_2.md](drivers/space_harrier_2.md) · [outrun.md](drivers/outrun.md) | Other SMPS drivers: how each differs from Sonic 1 |
| [streets_of_rage_2.md](drivers/streets_of_rage_2.md) | Mucom-MD 0.85 |
| [time_trax.md](drivers/time_trax.md) | Tim Follin's Z80 driver |

### Developer and reference

| Page | Contents |
|------|----------|
| [developer/architecture.md](developer/architecture.md) | Code structure, layers, `convert()` step order, MOD binary layout, tests, where to change what |
| [reference/mod_effects.md](reference/mod_effects.md) | Every ProTracker effect, and which ones sonic2mod writes |
| [glossary.md](glossary.md) | Every term these docs use |
| [audits/](audits/) | Per-song accuracy audits against the rips |
