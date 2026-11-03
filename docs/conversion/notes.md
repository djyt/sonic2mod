# Notes: range, transpose, routing

Which MOD instrument and MOD note each SMPS note becomes: fitting eight octaves into ProTracker's three,
routing a voice's notes to instruments by range, and anchoring them with `root`.  For anyone writing a
song config's `voice_map`; the keys themselves are in [../guide/song_configs.md](../guide/song_configs.md).

**On this page**

- [SMPS range to MOD range](#smps-range-to-mod-range)
- [`voice_map` routing](#voice_map-routing-resolve_note)
- [`root`](#root)
- [`range_space: chip`](#range_space-chip)

## SMPS range to MOD range

SMPS notes are bytes $81–$DF: semitone `byte − $81`, C0 to A#7, nearly eight octaves.  A MOD has
three, C1–B3 (MOD indices 0–35).  So every note is moved into that span: shifted by a transposition (the
[transpose path](../glossary.md#transpose-path)), or placed relative to a `root` anchor (the
[root path](../glossary.md#root-path)).  A note that still lands outside 0–35 is **clamped** and warned
(`clamp_high` / `clamp_low`), never silenced.

| Source notes | Semitones | Transpose to land on C1–B3 |
|--------------|-----------|----------------------------|
| C0–B2 | 0–35 | 0 |
| C3–B5 | 36–71 | −36 (most FM) |
| C4–B6 | 48–83 | −48 |

A channel spanning more than three octaves needs `voice_map` ranges, each with its own instrument (a
sample rendered for that octave span).  FM labels are real pitches (`nA4` at pitch offset 0 is 440 Hz);
a PSG byte indexes the driver's table, whose `nC0` is C3 ([../synthesis/psg.md](../synthesis/psg.md)).

## `voice_map` routing (`resolve_note`)

`resolve_note` (`core/plan/driver_state.py`) is the one place a note's instrument and MOD note are
decided; every pass calls it.

```
key = note − $81 (range_space: source)   or   the chip's pitch (range_space: chip)
                │
                ▼
ranges = channel_instrument_map[channel][voice]  or else  voice_map[voice]
                │
     entry with low ≤ key ≤ high?
     ├── yes: instrument = entry.mod_instrument (or its detune variant)
     │        ├── root set:  MOD note = root + (key − low)                        root path
     │        └── no root:   MOD note = (note − $81) + driver transpose + transpose   transpose path
     └── no:  the channel's instrument, MOD note as "no root"; a voice with ranges warns map_gap
```

- **driver transpose** = the header's pitch offset plus every `smpsChangeTransposition` ($E9) so far;
  **transpose** = the channel's YAML `transpose`.
- `channel_instrument_map` replaces the voice's `voice_map` list for that channel; it does not fall
  through to it.
- PSG: a multi-entry `psg_voice_map` list dispatches per note on `low` / `high` (that entry stays
  active); `root` with `low` places a tone as above, `root` alone pins a noise note; noise mode routes
  through `psg_map` ([../synthesis/psg.md](../synthesis/psg.md)).
- `smpsDetune` never takes part: it is a frequency-number offset, not semitones ([detune.md](detune.md)).

### Worked example

Green Hill's voice $05, low range (`configs/sonic_1/02_green_hill_zone.yaml`):

```yaml
voice_map:
  5:
    - low:  C4
      high: B5
      mod_instrument: 9
      root: C2
```

A note `nE5` (byte $C1, semitone 64) on voice $05:

```
key        = $C1 − $81 = 64 = E5           inside C4 (48) … B5 (71): entry found
instrument = 9
MOD note   = root + (key − low) = C2 + (64 − 48) = C2 + 16 semitones = E-3   (MOD index 28)
```

The whole range C4–B5 lands on C2–B3, inside C1–B3.  Without `root` the note would take the transpose
path: 64 + driver transpose + channel `transpose`; with both 0 it would clamp to B-3 (`clamp_high`).

## `root`

`root` is **unconditional**: `low` always plays at `root`, whatever the pitch offset, detune or
`smpsChangeTransposition`.  The sample's rendering pitch ([`synth_root`](../glossary.md#synth_root)) never
moves a note: its difference from the pitch `root` sounds ([`synth_shift`](../glossary.md#synth_shift))
goes into the sample's rate ([../synthesis/fm.md](../synthesis/fm.md)).

- **Use it** where a channel's key is fixed: the anchor is exact and independent of `transpose`.  When
  the entries cover every note of a channel, set `transpose: 0`.
- **Don't** on a channel that changes key with `$E9` mid-song: the same byte must reach different
  notes, and `root` places them all alike.  Use the transpose path (no `root`), or `range_space: chip`.
  The same holds for a PSG entry with `low` (Sonic 1's Labyrinth Zone PSG1 / PSG2 walk with
  `smpsAlterPitch`: rootless, `transpose: -12` on the channels).
- **Choosing it:** keep `root + (high − low)` within C1–B3, and prefer the highest such `root`: the
  sample plays at `root`'s rate, so a higher `root` keeps more treble (C2 ≈ 8.3 kHz, C3 ≈ 16.6 kHz at
  the PAL Paula clock).

> **Pitfall:** `root` with `$E9` key changes under the default `range_space: source` plays every key's
> notes at the first key's pitches.

## `range_space: chip`

A config's [**range space**](../glossary.md#range_space) says what `low` / `high` are matched against.  By
default (`source`) ranges match the source byte.  With `range_space: chip` (song level) they match the
pitch the chip plays: byte + pitch offset + accumulated `$E9`, a PSG note through the driver's table
(`chip_pitch`, `core/smps/driver_tables.py`, which also gives the hardware's pitch for notes transposed
past the table's ends).  So:

- `low` / `high` are chip pitches, and `root` always sounds `low` (never `synth_root_ambiguous`);
- a voice spanning more than three octaves gets one entry per [window](../glossary.md#window);
- two voices sharing one sample keep separate entries: `root_e = root_head + (low_e − low_head)`.

Needed where a song changes key while keeping a voice: Sonic 1's Credits FM2 does it twenty times;
matched on source bytes, only 8 % of its notes would land right.

`tools/config/config_to_chip_space.py <config>` converts a source-space config: for every entry it
collects the chip pitches each channel plays through it (loops extended, every transposition), writes
one entry per range touched with `synth_root` = its low note and `root` shifted to keep the tuning, and
warns where a range cannot fit C1–B3 at that tuning (those notes need an instrument of their own:
Sonic 1's Ending PSG2).  It rewrites the config's `voice_map`, `psg_voice_map` and
`channel_instrument_map` in place (their comments become `# was low X` notes); run the pitch audit after:

```bash
python tools/config/config_to_chip_space.py configs/sonic_1/05_lab_zone.yaml
python tools/vgm/vgm_pitch_audit.py configs/sonic_1/05_lab_zone.yaml "reference/vgz/sonic_1/05 - Labyrinth Zone.vgz"
```
