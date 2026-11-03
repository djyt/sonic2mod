# The conversion at a glance

How a song read from a ROM or assembly becomes a ProTracker MOD: what the converter does in
what order, and the limits of the MOD format every rule works within.  For anyone tuning a song config
who wants to know why the MOD looks the way it does, or anyone changing the converter.  Each rule has
its own page ([The conversion pages](#the-conversion-pages)).

## The flow

```
 .asm / ROM / .vgz
        │  read_song (core/source)
        ▼
    SmpsSong ──── SmpsToModConverter.convert() (core/convert/smps2mod.py) ────► ModFile ── get_bytes() ──► .mod
```

Each chip channel of the song (FM1, PSG2, DAC ...) becomes one MOD column: its `channels:` entry's
`mod_channel`.  Every step reads a channel the same way: one state machine (`DriverState`, advanced by
`walk_channel`) follows the driver note by note, and `resolve_note` decides which instrument and MOD
note each note plays (`core/plan/driver_state.py`).  So no two steps can disagree about a note.

### One pass over the song

In plain words: decide which samples are needed, re-time the song, render the samples, then write every
channel's notes and effects into the patterns.

```
1. rendering pitches, detune variants   which samples, at which pitch                  (detune.md)
2. song prepared                        tempo dividers applied, short loops replayed   (timing.md,
                                                                                         song_structure.md)
3. merge plan                           merged build only: which channels fold         (../guide/amiga_builds.md)
4. sustain needs                        how long each sample must ring                 (samples.md)
5. samples                              FM and PSG rendered, disk samples loaded, DAC
                                        drums saturated; merged: composites mixed and banked
6. every channel written                notes, effects, levels                         (notes.md, effects.md,
                                                                                         levels.md)
7. row 0                                leading rests' C00, then BPM / speed Fxx;      (timing.md)
                                        mid-song tempo Fxx
```

### Then the layout, in this fixed order

```
pattern breaks → loop Bxx (or a stopping song's D00) → trailing patterns trimmed
  → (merged) narrowed to the columns in use → sample names → (compact_slots) slots renumbered
  → (trim_tails) one-shots cut to what is heard → one-shots' first word zeroed (samples.pt_zero_bytes)
```

The loop's `Bxx` must land on the post-break layout, so breaks always come first
([song_structure.md](song_structure.md)).  A merged build with `merge_bank_slots: auto` may run the
whole pass again, up to four builds ([../guide/amiga_builds.md](../guide/amiga_builds.md#sample-banks)).
The code behind each step: [../developer/architecture.md](../developer/architecture.md#the-converter-convert-step-order).

> **Note:** The [reference build](../glossary.md#reference-build) (every chip channel on a column of its
> own) is the ground truth; the [merged build](../glossary.md#merged-build) (`--merged`) folds it to four
> columns for the Amiga.  These pages cover the rules both share.

## MOD limits

| Resource | Limit |
|----------|-------|
| Instruments | 31 |
| Patterns / positions | 127 (`max_patterns`) |
| Rows per pattern | 64 |
| Channels | 4, 8, 10, 12, 14, 16 (format tag) |
| Notes | C1–B3, 36 semitones |
| Effects | one per cell |
| Sample | 131070 bytes in the format, 65534 in the original ProTracker editor.  `samples.max_sample_kb` caps what the generators render (code default 128, shipped 64); `sample_truncated` warns if one is cut anyway |
| BPM | 32–255 (`Fxx` ≥ $20) |
| Speed | 1–31 MOD ticks per row |

Most rules follow from three of these: one effect per cell ([effects.md](effects.md#effect-priority)),
three octaves of notes ([notes.md](notes.md)) and a whole-number BPM ([timing.md](timing.md#bpm)).

## The conversion pages

| Page | What it covers |
|------|----------------|
| [timing.md](timing.md) | ticks, rows and patterns; `ticks_per_row`, `row_grid: bar`; the BPM formula; frames versus ticks; tempo changes |
| [notes.md](notes.md) | the SMPS note range on C1–B3; `voice_map` routing; `root`; `range_space: chip` |
| [effects.md](effects.md) | SMPS → MOD effect table; effect priority; `EDx`; note fill; vibrato; legato |
| [levels.md](levels.md) | baked FM and PSG levels; normalised volumes |
| [song_structure.md](song_structure.md) | loop extension, leading rests, the loop, a song that stops, pattern breaks |
| [detune.md](detune.md) | detune variants; the hardware LFO |
| [samples.md](samples.md) | sample length, tail trimming, sustain loops, release slides, sliding loops |

Related: the merged build [../guide/amiga_builds.md](../guide/amiga_builds.md); checking a MOD against a
rip [../guide/verifying.md](../guide/verifying.md); every config key [../guide/song_configs.md](../guide/song_configs.md);
the driver [../drivers/sonic_1.md](../drivers/sonic_1.md); sample rendering [../synthesis/fm.md](../synthesis/fm.md),
[../synthesis/psg.md](../synthesis/psg.md); code layout [../developer/architecture.md](../developer/architecture.md);
ProTracker effects [../reference/mod_effects.md](../reference/mod_effects.md).
