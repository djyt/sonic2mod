# Levels

How the chip's note levels (FM total level, PSG attenuation, pan) become MOD sample volumes and `Cxx`
(set volume), and how the whole song is raised to full MOD volume.  For anyone setting `sample_list`
volumes or reading why a note carries a `Cxx`.  Measuring levels against a rip:
[../guide/verifying.md](../guide/verifying.md#per-instrument-levels).

## FM (`fm_volume_scaling: baked`)

On the chip a note's level is its TL offset (`smpsHeaderFM` volume plus every `smpsAlterVol`, 0.75 dB
per step) and its pan: a hard-panned note counts 3 dB quieter (`fm_pan_law_db`), because a centred
YM2612 channel drives both speakers while an Amiga channel drives one.

A MOD note plays at its instrument's volume unless `Cxx` overrides it, and a second copy of a sample at
another volume costs its full size.  So each instrument gets one [**baked level**](../glossary.md#baked-level):

- `LevelPlanner.levels("FM")` (`core/convert/level_plan.py`) walks every FM channel with the
  conversion's `DriverState` and counts, per MOD instrument, notes per level `−0.75 × TL − pan`.  The
  most common level (ties: the louder) is the instrument's baked level; its `sample_list` volume stands
  for it and those notes get no command.
- Any other note gets `Cxx = volume × 10^(ΔdB / 20)` (× the channel's `volume / 64`, clamped to 64).
- The sample is **rendered at** that TL offset (`LevelPlanner.fm_render_levels`), so the chip clips a
  multi-carrier voice as the hardware does at that level ([../synthesis/fm.md](../synthesis/fm.md)).
- A detune variant votes as its instrument; a merged build's unison gain is part of the level.
- The laws are in `core/chips/` (`fm_level_db`, `psg_level_db`); dB → MOD volume is `core/mod/volume.py`.

Worked (illustrative): an instrument with `sample_list` volume 24, baked at the level most of its notes
play at; one channel plays it 4 `smpsAlterVol` steps quieter:

```
ΔdB  = −0.75 × 4         = −3 dB
Cxx  = 24 × 10^(−3/20)   = 17.0   → C11 (17 in hex) on each of that channel's notes
```

With the law right, every channel sharing an instrument shows the **same** error against the VGZ, which
one `sample_list` volume fixes.  If channels disagree, it is not a volume problem.

Legacy modes: `absolute` (or `true`: header TL as an absolute volume, `Cxx` on every FM note) and `off`
(or `false`: one `smpsAlterVol` step = one linear MOD volume unit, ≈ 0.3 dB; wrong by several dB on
faded notes).

## PSG (`psg_volume_scaling: baked`)

The same scheme on the SN76489: level = −2 dB × attenuation (`smpsHeaderPSG` volume +
`smpsPSGAlterVol`, 15 = silent; a silent note neither votes nor sounds), no pan term, planned by
`LevelPlanner.levels("PSG")` with the conversion's PSG instrument tracking.  Most PSG notes then carry
no `Cxx`, which leaves the slot to vibrato and in-row cuts.

Legacy mode `absolute`: `Cxx` on nearly every PSG note.

## Normalised volumes (`normalise_volumes: reference`)

The `sample_list` volumes are relative: a DAC drum starts at 64, everything else below it, and the
song's loudest note often plays well under 64 (Streets of Rage 2's Spin on the Bridge: 16).  A quiet MOD
wastes the player's volume range, so the [reference build](../glossary.md#reference-build) raises every
volume by one gain until that note plays at 64 (`core/convert/normalise.py`):

```
sample_list    4   12   44   52 (drum)       loudest note asks for 52
gain           64 / 52 = +1.8 dB
written        5   15   54   64              every Cxx, slide and fade from the raised volumes
```

- The loudest note is what the channel writers ask for before clamping (`EmissionStats.peak_volume`),
  so the channels are written once into a scratch MOD (`ModFile.scratch`), measured, and written again
  after the sample volumes are raised.  A note already clamped at 64 leaves the song as it is.
- Quiet instruments gain resolution: a PSG level at volume 1 could not tell its 2 dB steps apart; at 4
  it can, and their `Cxx` take the slot from a `4xy` ([effects.md § Effect priority](effects.md#effect-priority)).
- The config keeps its own volumes: `vgm_compare.py --write-volumes` writes each suggestion as the config
  row times its share of the MOD's volume ([../guide/verifying.md](../guide/verifying.md)).
- Never the merged build: a game engine's sound-effect levels are set against its volumes.  The
  report's MOD line says `volumes +1.8 dB`.

The keys (`fm_volume_scaling`, `psg_volume_scaling`, `fm_pan_law_db`, `normalise_volumes`) default to
the behaviour above; values and defaults: [../guide/settings.md](../guide/settings.md#top-level).
