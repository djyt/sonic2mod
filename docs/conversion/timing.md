# Timing

How SMPS time (driver ticks, V-int frames) becomes MOD time (rows, BPM, speed), and why the BPM must be
a whole number.  For anyone choosing `ticks_per_row` / `target_speed` in a song config or touching
`core/plan/timeline.py`.

**On this page**

- [Ticks, rows, patterns](#ticks-rows-patterns)
- [Choosing `ticks_per_row`](#choosing-ticks_per_row)
- [`row_grid: bar`](#row_grid-bar)
- [BPM](#bpm)
- [Frames versus ticks](#frames-versus-ticks)
- [Mid-song tempo changes](#mid-song-tempo-changes-smpssettempomod-ea)
- [Global duration divider](#global-duration-divider-smpssettempodiv-eb)
- [Tempo commands on row 0](#tempo-commands-on-row-0)

## Ticks, rows, patterns

The driver counts note lengths in [**ticks**](../glossary.md#tick), steps of its song clock (about one
a frame).  A MOD counts in **rows**, 64 to a pattern.  `ticks_per_row` sets the grid: how many duration
units one row holds.  The parser stores durations already multiplied by the header's
[**tempo divider**](../glossary.md#tempo-divider), so in stored ticks a row is `ticks_per_row × divider`
(`Timeline.ticks_per_row`):

```
row_total = tick / (ticks_per_row × divider)      pattern = row_total // 64      row = row_total % 64
```

- A **note-on** goes on the row it starts in (floor), delayed with `EDx` when it starts between rows
  ([effects.md § EDx](effects.md#notes-that-start-between-rows-edx)); without a free slot for the delay
  it is rounded.
- Everything else (rests, cuts, tempo changes, the loop target) rounds to the nearest row
  (`Timeline.pattern_row`).
- `merge_patterns:` pattern numbers count note-ons by floor, shifted past the pattern breaks
  (`Timeline.pattern_of`).

Example: Green Hill (`ticks_per_row: 2`, divider 1), a note at tick 389 → row_total 194 →
pattern 3, row 2, with an `EDx` for the odd tick.

## Choosing `ticks_per_row`

The greatest common divisor (GCD) of the song's note durations puts every note on a row.  A coarser
grid still works (notes between rows take `EDx`) and makes fewer patterns; a finer one wastes rows.
Every [tempo segment](../glossary.md#tempo-segment)'s BPM must also fit 32–255: Sonic 1's Drowning
needs `ticks_per_row: 2`.

| Common durations | GCD | `ticks_per_row` |
|------------------|-----|-----------------|
| $06, $0C, $18 | 6 | 6 |
| $04, $08, $0C | 4 | 4 |
| $04, $06, $0C | 2 | 2 |

## `row_grid: bar`

The default grid (`row_grid: accurate`) is the finest the notes need: right for checking against a
rip, but hard to read in a tracker.  [`row_grid: bar`](../glossary.md#row_grid) (usually set in a
variant) lays the song out as a composer would, a 16th note a row and whole phrases a pattern
(`core/plan/meter.py`).  It reads the meter from the notes that key on:

| Measure | How it is found |
|---------|-----------------|
| step | the coarsest grid that 85 % of the notes fall on; each channel may sit at its own offset (an echo a frame late) |
| row | the step, halved while more than 2 % of the notes (legato ones too, not glide or sweep steps) would share a row with their channel's previous note |
| phrase | the shortest lag at which the pitches repeat nearly as well as at the best lag: a bar or a few |
| downbeat | the loop's start, else the offset most notes fall on: it becomes a pattern's row 0 |

Patterns hold whole phrases: 64 rows where phrases are a power of two rows, else breaks every
`k × phrase` rows (Time Trax's Stages 2, 5, 7: 20-row 5/4 bars, 60-row patterns).  Notes between rows
take `EDx`, exact where the speed puts every frame on a MOD tick; where the cell's effect slot is taken
(a gate's `ECx`, a `Cxx`, a legato `3FF`), they round to a row: a frame off.

It derives `ticks_per_row`, `target_speed` and `mod_pattern_breaks` and renumbers `merge_patterns`'
patterns: stating any of them is an error (`null` them in the variant).  The report's Tempo line names
the phrase, or what gave way.

```yaml
# configs/time_trax/01_stages_2_5_7.yaml (a minimal config: nothing to null)
variants:
  bar:                  # rows by the song's meter: a 16th a row, whole phrases a pattern
    row_grid: bar
```

## BPM

The driver reads `fps × (m − 1) / m` ticks a second, *m* being the header's [**tempo modifier**](../glossary.md#tempo-modifier)
(`TempoWait` holds every *m*-th frame: [../drivers/sonic_1.md](../drivers/sonic_1.md)), and one duration
unit is `divider` ticks.  ProTracker plays `BPM / (2.5 × speed)` rows a second.  Equating rows per second:

```
BPM = fps × (m − 1) × speed × 2.5 / (m × divider × ticks_per_row)        fps = 60 NTSC, 50 PAL
```

Worked: Sonic 1's Special Stage, `smpsHeaderTempo $02, $08` (divider 2, *m* 8), `ticks_per_row: 2`,
`target_speed: 6`:

```
60 × 7 × 6 × 2.5 / (8 × 2 × 2) = 6300 / 32 = 196.875  →  FC5 (197 BPM)
```

| Song | divider | m | speed | tpr | BPM |
|------|---------|---|-------|-----|-----|
| Title Screen | 1 | 5 | 3 | 3 | 120 |
| Green Hill | 1 | 3 | 3 | 2 | 150 |
| Special Stage | 2 | 8 | 6 | 2 | 196.875 → 197 |
| Drowning | 1 | 2 | 2 | 2 | 75 (header tempo) |

A row lasts `speed × 2.5 / BPM` seconds: Green Hill's speed 3 at 150 BPM is 3 × 16.7 ms = 50 ms a row,
the same as its two driver ticks at 40 ticks a second.

`auto_bpm: true` computes the BPM (`derive_bpm`: rounded, clamped to 32–255; 150 when the modifier is
≤ 1) at the driver's frame rate (`update_hz`: its update clock on the `region`'s console).  Otherwise
`target_bpm` is used as written.  The speed `Fxx` is written only when `target_speed` is not 6
(ProTracker's default).

### The BPM is a whole number

A rounded BPM drifts against the hardware: Special Stage at speed 3 is 98.4375 → 98, 0.44 % slow,
139 ms behind over its 33 s pass.  `target_speed` changes the MOD ticks per row, not the row grid, so
choose the speed whose BPM is (nearly) whole: speed 6 above (+0.06 %).  `convert.py` prints the error
and the better speed; `analyze.py`'s skeleton and minimal configs pick it (`bpm_rounding_options`).

## Frames versus ticks

`TempoWait` holds every *m*-th V-int frame, so ticks are unevenly spaced: tick *k* of a tempo segment
falls on frame `k + k // (m − 1)`.  Green Hill (*m* = 3):

```
tick    0   1   2   3   4   5
frame   0   1   3   4   6   7        (frames 2 and 5 are holds)
```

The schedule is the song's (`SmpsSong.tempo_schedule`: the driver's phase, a segment starting the tick
after its `smpsSetTempoMod`); `Timeline.holds_before` places `EDx` by it.

Note fill and modulation count **frames**, not ticks ([../drivers/sonic_1.md](../drivers/sonic_1.md)).
Every frame count is put on the tick timeline with `Timeline.ticks_per_frame_at(tick)` = `(m − 1) / m`
for the modifier in force at that tick (1 for SFX), never multiplied by the tempo divider.  Example:
Green Hill's 2/3 tick a frame turns a 6-frame fill into 4 ticks, two rows.

## Mid-song tempo changes (`smpsSetTempoMod`, $EA)

The flag sets every track's modifier and restarts the hold counter.  `Timeline.collect_segments` splits
the song into [**tempo segments**](../glossary.md#tempo-segment); each change gets an `Fxx` with the BPM scaled by the change in tick
rate (`Timeline.bpm_for`) on its row (`ModLayout.tempo_changes`: a spare column first, then any cell
with no effect, then one holding only a `4xy` continuation).

- A loop back into another segment gets an `Fxx` at the loop target too.
- Fills, vibrato rates and `EDx` delays use the modifier at their tick.
- A segment whose BPM leaves 32–255 is clamped and warned (`tempo_bpm_range`).

> **Note:** Inherent, not a bug: the driver's holds fall at the end of each counter cycle, so the MOD
> is one to two frames behind after each change (Drowning ends 59 ms late).  The audit tools allow for it.

## Global duration divider (`smpsSetTempoDiv`, $EB)

`cfSetTempoDividerAll` writes every track's divider, and the driver multiplies a duration by it when
the note is read: a note begun before the change keeps its length, and the last write wins against a
track's own `smpsChanTempoDiv`.

`prepare_song` (`core/smps/song_prep.py`) re-times every channel before anything reads ticks (the
carrying channel first; the parser keeps `smpsChanTempoDiv` as an event so each note's divider is
known).  Rows stay ticks: Credits' half-tempo passage has twice the rows at the same BPM.  Loop labels
are not re-timed; no song that uses the flag loops.

## Tempo commands on row 0

The BPM and speed `Fxx` are placed after every channel is converted (`ModLayout.tempo_commands`), into
cells whose effect slot is free: spare columns first, then any column with no row-0 effect; failing
that a leading rest's `C00` gives way (warned `rest_no_slot` when the song loops to row 0).

Placed last because a note's own row-0 `Cxx` would otherwise overwrite them and the song would play
at the wrong speed.  In a merged build only columns that hold notes take them first: an `Fxx` on an
empty column keeps it from being narrowed away.
