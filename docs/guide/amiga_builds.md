# Amiga builds (the merged build)

A Mega Drive song plays on up to ten channels at once (six FM, three PSG tones plus noise, the DAC
drums); an Amiga MOD player has four.  The **merged build** (`convert.py --merged`) folds a song's
channels together until they fit: two parts that play together become one sample on one MOD channel.
This page is for whoever writes a song config's merge section: a step-by-step how-to on Sonic 1's
Title Screen and Green Hill Zone, then each rule the converter applies, with why.  Every merge key's
default is in [song_configs.md § Merged-build keys](song_configs.md#merged-build-keys); this page does
not repeat the table.

On this page:

- [Terms](#terms)
- [How-to](#how-to)
- [Groups](#groups)
- [The fill pool](#the-fill-pool)
- [Vacating a column](#vacating-a-column)
- [Composite instruments](#composite-instruments)
- [Per-pattern folds](#per-pattern-folds)
- [Choosing the primary](#choosing-the-primary)
- [Sample banks](#sample-banks)
- [Slots](#slots)
- [Other merged-build rules](#other-merged-build-rules)
- [Variants (lofi)](#variants-lofi)
- [Checking a merged build](#checking-a-merged-build)

---

## Terms

| Term | Meaning |
|------|---------|
| [**reference build**](../glossary.md#reference-build) | the plain conversion: one MOD column per Mega Drive channel.  The ground truth the merged build is made from and checked against; `--merged` never changes it |
| [**fold**](../glossary.md#fold) | playing two or more Mega Drive channels on one MOD column |
| [**primary**](../glossary.md#primary) | the channel a fold lands on: its column, its effects (vibrato, note fill, volume) |
| [**follower**](../glossary.md#follower) | a channel folded onto a primary: it leaves the output, its notes sound inside the primary's |
| [**composite**](../glossary.md#composite) | a sample made of a primary note plus the follower notes that start with it — a chord or a drum + hi-hat hit pre-mixed into one sample |
| [**solo note**](../glossary.md#solo-note) | a follower note placed on the primary's column, as itself, while the primary is silent |
| [**orphan**](../glossary.md#orphan) | a follower note that starts while the primary is still sounding, not on a primary note: it has nowhere to go |
| [**fill pool**](../glossary.md#fill-pool) | notes placed on whichever column is silent when they start, instead of a fixed one |
| [**bank**](../glossary.md#bank) | one MOD sample slot holding several short sounds back to back, each started with `9xx` (sample offset) |
| [**slot**](../glossary.md#slot) | one of the MOD's 31 instrument numbers; composites compete for them with the song's own instruments |

```
  Mega Drive channels                       merged MOD (4 columns)
  DAC  ●───●───●───●   (drums)       ┐
  PSG3 ●───●───●───●   (hi-hat)      ┘ fold ─►  col 1  [kick+hat] [snare+hat] ...
  FM1  ●━━━━━━━●━━━━━  (lead)        ┐
  FM5  ●━━━━━━━●━━━━━  (its double)  ┘ fold ─►  col 2  [lead+double]
  FM2  ●──●──●──●──●   (bass)          ──────►  col 3  bass, as in the reference build
  FM4  ●────●────●──   (chord)       ┐
  FM3  ●────●────●──   (3rd above)   ┘ fold ─►  col 4  [chord: FM4 + FM3 a third up]
```

---

## How-to

Sonic 1's Title Screen: seven channels to four.

### 1. Survey the pairs

`tools/config/merge_survey.py` lines up every ordered pair of channels the way the converter would and
counts what each fold keeps and loses:

```bash
python tools/config/merge_survey.py configs/sonic_1/01_title_screen.yaml          # the clean pairs
python tools/config/merge_survey.py configs/sonic_1/01_title_screen.yaml --all    # every pair
```

```
primary  follower notes paired solo cut alone orphan held short trunc vib comps  verdict
----------------------------------------------------------------------------------------
FM3      FM4         17     17    0   0     0      0    0     0     0   1     4  clean
FM4      FM3         17     17    0   0     0      0    0     0     0   1     4  clean
FM1      FM5         16     16    0   0     0      0    0     0     0   0     1  clean
FM5      FM1         16     16    0   0     0      0    0     0     0   0     1  clean
(38 pairs with losses hidden; --all shows them)

Suggested: 7 channels -> 4

merge:
  - primary: FM3
    followers: [FM4]
  - primary: FM1
    followers: [FM5]
  - primary: DAC
    followers: [PSG3]
```

The columns are the outcomes in [Groups](#groups).  `clean` loses nothing; `comps` is how many
composites (slots) the pair needs.  The suggestion takes the cleanest pairs first, each channel once,
and never a pair with orphans: here it adds DAC + PSG3, a pair with losses that `--all` lists.  It takes `--variant NAME` like every config tool.

### 2. Write the `merge:` groups

Paste the suggestion into the config and adjust by ear.  The shipped Title Screen
(`configs/sonic_1/01_title_screen.yaml`):

```yaml
merge:
  - primary: FM1
    followers: [FM5]        # FM5 is FM1's detuned double
  - primary: FM4
    followers: [FM3]        # FM3 plays a third / fourth above FM4
  - primary: DAC
    followers: [PSG3]       # the hi-hat lands on 24 of the 30 drum hits
    max_composites: 2       # kick + short hat, snare + long hat; the rest share these
    limit_db: 4             # peaks over full scale limited, not the mix turned down
```

### 3. Folds that change per section: `fold_csv.py`

When one song-wide fold does not fit the arrangement (Green Hill's bass rides the drum column in the
verse but has its own column in the intro), describe the folds per pattern in a CSV and let
`tools/config/fold_csv.py` write `merge_patterns:`.  Rows are reference-build patterns (hex, as Fast
Tracker shows them), columns the reference MOD's channels (`input/sonic_1/02_ghz_fold.csv`):

```
Pattern,Ch 1,Ch 2,Ch 3,Ch 4,Ch 5,Ch 6,Ch 7,Ch 8,Ch 9
,DAC Drums,Lead,Bass,,,,,,Hi-Hat
0,fold 1,keep,keep,keep,,,,,fold 1
1,fold 1,keep,fold 1,fold 2,fold 2,fold 2*,fold 2,keep,fold 1
...
```

A cell is `fold N` (channels with the same N share a column there), `fold N*` (and this one is the
primary), `keep`, `drop`, or blank (kept where the channel plays, else dropped).  Equal rows join into
one block.

```bash
python tools/config/fold_csv.py configs/sonic_1/02_green_hill_zone.yaml input/sonic_1/02_ghz_fold.csv           # print the YAML
python tools/config/fold_csv.py configs/sonic_1/02_green_hill_zone.yaml input/sonic_1/02_ghz_fold.csv --write   # write it into the config
```

| Flag | Effect |
|------|--------|
| `--write` | replace the block between the `# >>> merge_patterns` / `# <<< merge_patterns` marker comments (not with `--variant`) |
| `--bank` | `bank: true` on every drum-primary group ([Sample banks](#sample-banks)) |
| `--mix-note NOTE` | `mix_note: NOTE` on every drum-primary group (`F2`: mixes at 11 kHz instead of a hat's 28 kHz) |

The config stays the source of truth: edit the block by hand afterwards (Green Hill's shipped block
is hand-edited); a later `--write` replaces it from the table again.

### 4. Convert

```bash
python convert.py configs/sonic_1/01_title_screen.yaml --merged            # → output/sonic_1/01_title_screen_merged.mod
python convert.py configs/sonic_1/01_title_screen.yaml --merged --verbose  # also every composite, bank sound, loop
```

The output is `merge_output_file`, default `<output_file stem>_merged.mod`.  The report's Merge
section counts each fold:

```
 Merge   composite slots 7/28 · 7 wanted · 0 in banks
  Col   Fold         Composites   Folded   Solo   Lost   Cut
    1   DAC + PSG3            2       24             —     1
    2   FM1 + FM5             1       16             —     —
    4   FM4 + FM3             4       17             —     —
  instruments 3, 4, 6, 8 not rendered (no note of this build plays them)
  limit_db: 2 mixes' peaks limited, up to 1.2 dB
```

### 5. Check it

```bash
python tools/mod/mod_audit.py output/sonic_1/01_title_screen_merged.mod
python tools/vgm/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz" --merged
```

See [Checking a merged build](#checking-a-merged-build).  Then listen: which parts to keep and which
channel leads a fold are musical choices.

---

## Groups

A group (`merge:`, or a `merge_patterns:` block's `groups:`) names one primary and its followers.
The live channels are packed onto MOD columns 0..n−1 in their configured order.

For every primary note-on at tick *t*, the converter looks at each follower (its notes as the driver
plays them: an `smpsNoAttack` tie extends a note, a rest ends it; a drum or noise note sounds for its
sample or its note fill, whichever is shorter):

| Follower at *t* | Result | Survey column |
|-----------------|--------|---------------|
| note-on, same length | composite | `paired` |
| note-on, longer | composite; its tail is cut by the primary's next rest (`trunc`) or re-attacked by its next note (`held`) | `paired` |
| note-on, shorter | composite, the follower keyed off at its own length inside it — unless it ends within `merge_tolerance` of the primary | `short` |
| resting | the primary alone | `alone` |
| still sounding from earlier | the primary alone; the follower's ring is lost | `held` |
| note-on while the primary sounds, not on a primary note-on | lost — or, with `cut_primary: true`, played as a solo note that cuts the primary's tail | `orphan` / `cut` |
| note-on while the primary is silent | spliced onto the primary's column as itself (its instrument, note, level, fill) | `solo` |

**Onset tolerance.**  `merge_tolerance` (ticks, default 1): a follower note-on that close to the
primary's counts as at *t*.  A note that short followed by an `smpsNoAttack` note is a grace note:
the two count as one note at the pitch they land on, so a chord folds on its real shape.  Example:
Green Hill's FM3 starts its chord tone a tick after FM4 / FM5.

**The primary's effects win.**  The composite plays with the primary's vibrato, note fill, `Cxx` and
`EDx`.  A follower whose vibrato differs is counted (`vib`) but plays the primary's; a solo note has
no vibrato.  A short primary fill cuts the whole chord, so choose the primary with care
([Choosing the primary](#choosing-the-primary)).

**`cut_primary: true`** lets a follower note that starts over the primary's tail play and cut it.
Why: that is what a hi-hat does to a drum's decay on any four-channel Amiga; without it the hat is
lost.

**`max_composites: N`** keeps the group's N most-played composites; other chords play the primary
alone.  Why: a memory budget.  Example: the Title Screen's drums keep two (kick + short hat, snare +
long hat); the three one-off mixes would cost 18 KB.

**`merge_drop: [...]`** leaves channels out of the merged build.  Why: some songs have more
independent parts than four columns can hold; which to keep is a musical choice (Green Hill keeps
drums, bass, lead and one harmony).

**Followers and dropped channels are still walked.**  Their levels, envelopes and rendering pitches
are what composites and solo notes are made from; they only leave the output.  Instruments no note of
the merged build plays are not rendered.

**Two channels that never overlap** are a clean pair of solo notes: they simply share a column.

### A follower's note fill

The driver keys a follower off at its `smpsNoteFill` while the primary plays on (Green Hill's bass: a
67 ms pluck under every kick).  So the fill is part of the composite: a chip layer is keyed off early,
a mixed layer cut with its release.  A solo note keeps its own fill; a PSG solo note ends at its
length on whatever column it lands; an FM solo note on a PSG primary's column rings into its rest's
release.

---

## The fill pool

Some parts fold badly onto any one column but fit in the gaps between everyone's notes.  The fill
pool takes such notes and places each on **any** output column that is silent when it starts.

| Key | Pools |
|-----|-------|
| `merge_fill: [PSG1, PSG2]` | every note of those channels |
| group `fill_lost: true` | the follower notes the group cannot fold (orphans) |
| group `fill_cut: true` | follower notes whose ring the fold would cut (longer than the primary's note, with the primary's next note-on or rest inside it) |
| block group with no followers, `fill: true` | the primary's notes in that block's patterns ([Per-pattern folds](#per-pattern-folds)) |

How a note is placed:

- The pool runs before anything folds: a pooled follower note leaves its group, and the groups are
  paired again.
- A column is busy for its own notes' sounding time plus everything already spliced onto it.
- The column that stays silent longest wins: the whole note where one can, else one whose next
  note-on cuts it — never for less than a row.  A note with no silent column is lost.
- A `fill_cut` note moves only where a column is silent for all of it; otherwise it stays folded.
- `merge_fill_cut_after: {DAC: 2, FM2: 4}` makes that column's notes count for only that many ticks,
  so a pool note may cut a kick's decay or the second half of a bass note.  A column not named is
  never cut.  A block group's `cut_after: N` does the same for its column in its patterns.
- A placed note keeps its own instrument, level and pitch on whatever column it lands.

Example: Sonic 1's Invincibility lofi pools its hi-hat (`merge_fill: [PSG3]`): 14 of 48 hats find a
silent column, the rest start on a drum hit and are lost — chosen by ear over the 7.5 KB of drum +
hat mixes.

> **Note:** a part that starts on the other parts' note-ons cannot be pooled.  Green Hill's chimes
> start on a bass and drum hit almost every time (30 of 188 placed), so they fold onto the bass as
> bass + chime mixes instead.

The report gives, per source, notes placed where, cut, lost and kept folded.

---

## Vacating a column

`merge_vacate: [FM5]` frees a column as far as that loses nothing.  Why: a game that takes a channel
for sound effects.  Sonic 1's Robotnik lofi vacates FM5 because the Amiga port plays its effects on
channel 4: 28 of FM5's 84 notes move, the rest stay home.

The channel stays live; after the rest of the pool, each of its notes moves to the first column (in
column order) silent for all of it, else stays on its own column.

- A column's notes count whole, `merge_fill_cut_after` or not: a vacated note cuts nothing.
- No pool note lands on a vacated column, and vacating takes no column another pool note needed.
- A legato run (an `smpsNoAttack` note and the note it bends out of) stays home whole.
- Release slides are not counted: a moved note may cut the release of the column's note before it,
  and its own release ends at the column's next note.  Read the cells (a lost `A0y`) when the voices
  release slowly.
- A moved note plays from its new column's side of the Amiga's L R R L panning.

A vacated channel may be in no group, drop or pool.  A note moved off pattern 0 row 0 leaves a `C00`
there, so the loop does not come back with the column's last note ringing.

---

## Composite instruments

One composite per distinct shape.  The shape holds intervals, not notes, so the same chord at another
pitch plays the same composite at another MOD note.

### Chip composites

When every follower is an FM voice on an FM primary, the composite is rendered on the emulated
YM2612 like any instrument: each voice on its own chip channel at its interval, with its track's
detune, its carrier level relative to the primary's (a hard pan counts 4 steps), keyed off at its
fill.  The chip sums and clips the voices as the hardware does.

- Rendered at the level its own notes play most.
- Volume: the primary's `sample_list` volume × the composite's peak over the primary voice's alone ×
  a **speaker gain**, adjusted by how its level differs from the primary's in the reference build.
  Why the speaker gain: voices panned to opposite speakers sum in a mono render but never meet on the
  hardware (Sonic 1's 1-Up: FM3 left + FM5 right read 2.4 dB loud without it).  Past 64 the volume is
  clamped and the report's `merge_headroom` says by how much.

### Mixed composites

Anything else (a drum, a PSG note, a follower with a different chip) is mixed from rendered samples
once every sample exists.

| Aspect | Rule | Why |
|--------|------|-----|
| Trigger note | the fastest layer's note, capped by the group's `mix_note`; or the primary's own with `mix_at: primary` | a hat on a kick keeps its treble; `mix_note: F2` gives Green Hill's drum mixes 2.5× fewer bytes (no hat above 5.5 kHz); `mix_at: primary` keeps a looped primary's loop (followers resampled down into it) |
| Sources | each layer's unquantised render, resampled by its period ratio to the trigger note | quantised once, by the mixer or its bank |
| Follower fill | part of the shape: the layer is cut there and decays at the voice's release rate (2 ms fade where it has none) | the driver keys it off there |
| Length | the composite's longest note plus release padding (an FM primary's release; 50 ms for a PSG or drum primary) | a layer that outlasts it is keyed off with its release, never chopped (a hard cut clicks on every kick) |
| Loop | a looped primary mixed at its own rate keeps its loop, moved past the followers' tails; otherwise it plays straight through | |
| Level | the sum is peak-normalised and the volume set to its level; past full scale it stays at 64 (`merge_headroom`) unless the group's `limit_db` limits the peaks by up to that many dB | `limit_db` is denser, with a little transient distortion |
| End | cut (2 ms fade) where no note of it can be heard any more: its end plus release (until 48 dB down), or its column's next note-on, whichever comes first | bytes nobody hears; a kept loop no note reaches is dropped |

Group sound options: `treble_shelf_db` / `treble_shelf_hz` (a brightness shelf on the group's
composites, on top of any song shelf), `dither:`, `name:` (the MOD sample name).

**`fm_on_chip`** (default on): a mix with an FM primary and FM followers renders those FM voices
together on the chip, unlooped, for the longest note it plays, and mixes only the rest (a PSG, a drum)
on top.  Why: the voices' detune and phase run on as the hardware's do, instead of two looped samples
repeating every few tens of ms.  The render takes the primary's loop away: `mix_at: primary` keeps
none, and a long mix loops only with `loop_mix`.

**`loop_mix: true`**: a long mix with no loop loops where its sum settles, with an 80 ms crossfade;
`loop_drift_db` / `loop_min_ms` / `loop_start_ms` steer it.  Lossy: the chord's slow movement freezes
in the loop.  Green Hill lofi's chords: a 0.73 s loop, 11.7 KB saved.

---

## Per-pattern folds

A song-wide group folds a follower everywhere.  When sections want different folds, give the groups
per block of patterns (Green Hill, `configs/sonic_1/02_green_hill_zone.yaml`):

```yaml
merge_patterns:
  - patterns: "0"
    drop: [FM4, FM5, PSG1, PSG2]  # left out in these patterns
    groups:
      - primary: DAC
        followers: [PSG3]
        cut_primary: true
        bank: true
        mix_note: C3
  - patterns: "1-4"
    groups:
      - primary: DAC
        followers: [FM2, PSG3]    # the bass rides the drum column in the verse
        cut_primary: true
        bank: true
      - primary: FM5
        followers: [FM3, FM4, PSG1]
        mix_at: primary
        mod_channel: 1            # take column 1 here
      - primary: PSG2
        mod_channel: 2            # no followers: a plain move
```

- **Pattern numbers** are the reference build's, after its `mod_pattern_breaks`, written as hex
  strings: `"0"`, `"1-4"`, `"d-10"`, `"0, 5-c"`.  A YAML integer is decimal.  A note belongs to the
  pattern its note-on lands in.
- A channel may follow in one block, lead in the next and keep its own column in a third.  It leaves
  the output only if it is a follower or dropped in every pattern it plays in.  The output keeps one
  column per channel that is live anywhere (Green Hill: 8 of 9), each keeping its meaning for a hand
  finish in a tracker.
- In the patterns where a channel follows, its own column writes nothing; the first folded note-on
  ends whatever of its own still rings there.
- A fold lands on its primary's column.  `mod_channel:` (a `channels:` number or a source name) moves
  the primary to that column in the block; the column must be free there.  Two sources on one column
  in a pattern is an error naming it.
- A note left ringing on another column when the block changes is cut there.
- A group with no followers and `fill: true` pools its primary's notes in the block (the channel has
  no column there).  Green Hill's bridge arpeggio is sprinkled over the four columns this way.
- Two groups may share a primary in different blocks; an identical chord in two blocks shares one
  composite.  A song-wide `merge:` may sit beside `merge_patterns:`.
- Errors and reports: a channel claimed twice in one pattern is an error; a departed channel's notes in
  patterns no group folds are lost (`merge_dropped`); patterns no block names fold nothing
  (`merge_unspecified`).

---

## Choosing the primary

A fold's primary decides its column and its effects.  `fold_csv.py` cannot read it from the table, so
it tries every member and prints the counts:

- **The drums own any fold they are in.**  Every note cuts a drum's decay on an Amiga anyway, and the
  fold stays on the drum column.
- **Otherwise the member that plays the fewest follower notes wrong:** lost ones, plus paired ones the
  primary's own effects would distort (a primary fill shorter than the follower's note cuts the
  composite; the primary's vibrato becomes the composite's).  Example: a chime with a 16-frame fill
  and vibrato never leads a chord it rides.
- `fold N*` in the table overrules the measurement by ear.

---

## Sample banks

Mixed composites are many short sounds, and slots run out.  A group with `bank: true` packs its mixes
into shared slots: one sample holds several sounds, and each note picks its sound with `9xx` (start at
offset xx × 256 bytes, up to `$FF00`).

```
 one slot, one bank (offsets illustrative):
 |kick+hat ....|snare+hat .......|kick+hat(long) ...|bass+hit ~~~~ loop|
 ^ 900          ^ 90C              ^ 91F              ^ 935
   each sound starts on a 256-byte boundary; silence pads to the next one
```

- Most-played first.  Each sound starts on a 256-byte boundary, followed by one MOD tick of silence
  (at the slowest tempo) up to the next boundary.  One finetune per bank.
- A bank plays at its loudest member's volume; quieter members are scaled into their bytes (losing
  8-bit range), so sounds are grouped by volume where that takes no more banks.
- A looped mix goes last in its bank (the bank's loop is its loop) and its notes need no cut.
- Two sounds that are one (same length, finetune and volume, sums within half an LSB) share one
  region.  Green Hill lofi's drum + bass hits whose shapes differ only in a fill after the hit: 3 KB.
- Chip composites are never banked.
- A member that fits nowhere is dropped like any composite over budget and reported
  (`merge_bank_dropped`).

**In the output** every banked note starts with `9xx` and, unless the column's next note-on comes
first, is cut once its sound is over (`C00` on a row, `ECx` inside one) — otherwise it would play on
into the next sound.  A melodic primary banks too; the `9xx` takes the attack row's effect: a `Cxx`
moves to the note's next free row, an `EDx` is dropped, a cut in that row moves a row on, and a
no-attack note is re-triggered.

**`bank_drums: true`** (a drum primary's group; implies `bank`, needs no followers) banks the drums
themselves: a hit with no follower is a one-layer composite, so no note plays a drum's own slot and
those slots become banks.  Example: Space Harrier II's Harrier Saga
(`configs/space_harrier_2/81_harrier_saga_4ch.yaml`): five drum slots become two banks, FM4's
arpeggio rides the drum column as drum + FM4 mixes, and six channels fit four.

**`merge_bank_slots`**: slots held back from the composites for the banks (banks also take any slot
left free).  `auto` (default): the conversion reruns, up to four builds, with the reserve the banks
turned out to need.  A number pins it; a held slot left empty goes back to the composites once.

---

## Slots

A MOD has 31 instrument slots.  Composites share them with the instruments the merged build still
plays.

- **Offered:** slots no `sample_list` / map names, then slots of instruments no merged note plays.  A
  drum's slot is never reused (it is loaded from disk), except a `bank_drums` group's, offered last.
  A slot whose instrument only feeds mixes can be reused: its sample is rendered anyway and kept
  aside.  An FM instrument's slot takes only a mixed composite.
- **Order:** each group's `max_composites` first, then the most-played composites.
- **Over budget:** composites are dropped — same-shape twins first, then those whose primary
  instrument is played anyway, then the least played — and the fit is redone, since a dropped
  composite hands its notes back to the primary's instrument.
- **A composite owns its slot:** the instrument formerly there is gone from this build.
- Report: `composite slots N/M · K wanted`; `merge_unsupported` lists notes that play the primary
  alone.

Three rules keep one sound out of two slots:

- **A unison is the primary, louder.**  A chord whose every follower is the primary's own voice at
  the same pitch, no detune, ending with it, makes no composite: the note plays the primary's
  instrument with the followers' gain baked into its level (+6 dB for an equal pair on one speaker,
  +3 dB for left + right; reported `merge_unison_volume`).  A detuned unison beats, so it stays a
  composite.  Any instrument whose commonest level moves in the merged build has its volume moved
  the same way ("level moved"), so every note keeps the reference build's volume.
- **[Twins](../glossary.md#twin) give up their slot first.**  Composites of one shape (same voices and
  intervals, differing only in fill or level) are twins.  The one whose followers ring furthest, then
  the most played, is kept if it can play every note of the other.  `merge_twins: always` (default
  `short`: only while slots are short) drops twins even when everything fits, for the bytes.
- **[Stand-ins](../glossary.md#stand-in).**  A composite dropped for any reason plays a surviving one
  of its shape instead of losing the follower.

---

## Other merged-build rules

| Rule | Detail |
|------|--------|
| Rendering pitch | `merge_max_synth_shift` (default 12) caps how far above its root a sample is rendered; 0 halves every shifted sample, at the cost of envelopes running faster up the range ([synthesis/fm.md](../synthesis/fm.md)) |
| Loops | `samples.sustain_loops: merged` loops this build only; its loops skip the timbre check unless `merge_loop_timbre: true` ([settings.md](settings.md)) |
| Four columns | a merged build whose columns fit four is written as a 4-channel `M.K.` file |
| Slot numbers | renumbered from 1 without gaps (`samples.compact_slots: merged`, shipped); the reference build keeps the config's numbers, which `sample_list` tuning is keyed on |
| Tails | `samples.trim_tails: merged` (shipped) cuts each sample after the last byte any note can be heard to |
| Player | `merged_player` (shipped `pt2`: ProTracker 2, the Amiga's replayer) sets vibrato depths; `--player` overrides |
| Volume | `normalise_volumes: reference` (shipped) never raises the merged build: its levels are set against the game's sound effects |
| Output | `merge_output_file`, default `<output_file stem>_merged.mod` |

---

## Variants (lofi)

A smaller build that differs in a few keys is a `variants:` block, not a copied config
([song_configs.md § Variants](song_configs.md#variants)).  Sonic 1's `lofi` variants squeeze the merged
build for the Amiga's memory: Green Hill's merged build is 168 KB as lofi.

```yaml
variants:
  lofi:
    merge_twins: always         # one mix per chord shape
    merge_max_synth_shift: 0    # every sample at its root pitch
```

A group's own keys take a `variants:` block too (`lofi: {mix_note: A2}`).

```bash
python convert.py configs/sonic_1/02_green_hill_zone.yaml --variant lofi --merged
# → output/sonic_1/02_green_hill_zone_lofi_merged.mod
python convert.py configs/sonic_1 --variant lofi --merged     # every song that has one
```

---

## Checking a merged build

| Question | Tool |
|----------|------|
| Where do the bytes go?  Any sample too short, unused, duplicated? | `python tools/mod/mod_audit.py <mod> [--banks]` |
| Is each column as loud as the channels folded onto it, with the hits on time? | `python tools/vgm/vgm_compare.py <config> <vgz> --merged` |
| Can a ProTracker player sound every note? | `python tools/mod/mod_lint.py <mod>` |
| Does a size cut sound the same? | `python tools/mod/mod_render_diff.py before.mod after.mod` |
| Did a code change move it? | `python tests/regression.py` (every config with a merge section is also a `<name>_merged` case) |

- **`mod_audit.py`** reads any MOD and reports each sample against the notes that play it: unused,
  too short, oversize (`--slack`, default 2 s past the longest note), empty slot, low rate, `same as N`
  (first 100 ms correlate ≥ 0.98), `finetune variant of N`; `--banks` lists each bank sound (its `9xx`
  offset, bytes, notes, seconds heard).
- **`vgm_compare.py --merged`** renders each merged column and, with a combined mute mask, the sum of
  the chip channels folded onto it; it reports whole-song balance and onsets per column.  With
  `merge_patterns:` each column's reference is built per pattern from the channels that sound there: a
  column × pattern-block table of block level and the primary's key-on attacks against the song's
  median block, flagged at 2 dB.  Pooled notes are in no reference.  There is no per-note audit: that
  needs one note stream per channel, so it is the reference build's ([verifying.md](verifying.md)).
- **`mod_render_diff.py`** renders both MODs per channel and reports the worst 20 ms difference.
  Rebuilt samples carry fresh dither seeded by their length, so a sample that only got shorter still
  differs by about −37 dB; hold the seed fixed in both builds to see past it.
