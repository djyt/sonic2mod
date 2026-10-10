# sonic2mod Conversion Pipeline

The rules by which an SMPS song becomes a ProTracker MOD: timing, note placement, effects, levels,
sample length, the merged (Amiga) build, and how a conversion is checked against a VGZ recording.
What the driver does is `smps_driver.md`; how samples are rendered is `fm_synthesis.md` /
`psg_synthesis.md`; config keys are `yaml_config.md`; module structure is `architecture.md`;
ProTracker effects are `mod_effects.txt`.

**Contents**

1. [The conversion at a glance](#the-conversion-at-a-glance)
2. [Timing](#timing)
3. [Notes: range, transpose, routing](#notes-range-transpose-routing)
4. [Effects](#effects)
5. [Levels](#levels)
6. [Song start, loop and end](#song-start-loop-and-end)
7. [Detune variants](#detune-variants)
8. [Sample length, sustain loops and release slides](#sample-length-sustain-loops-and-release-slides)
9. [Pattern breaks](#pattern-breaks-mod_pattern_breaks)
10. [The merged build](#the-merged-build)
11. [Verifying against a VGZ](#verifying-against-a-vgz)

---

## The conversion at a glance

```
.asm / ROM / .vgz ─ read_song ─► SmpsSong ─ SmpsToModConverter.convert() ─► ModFile ─ get_bytes() ─► .mod
```

Each SMPS channel becomes one MOD column (its `channels:` entry's `mod_channel`).  Every pass reads a
channel the same way — `walk_channel` advancing one `DriverState` and `resolve_note` deciding what each
note plays (`architecture.md`) — so the passes cannot disagree.

**One pass** (`_convert_once`):

1. rendering pitches and detune variants (`prepare_instruments`), on the song as parsed;
2. `prepare_song`: a new song, tempo dividers applied, then short loops replayed (the song given is left as it is);
3. merged build only: the merge plan (`build_merge_plan`);
4. sustain needs (`SustainPlanner`);
5. samples: FM rendered, disk samples loaded, DAC drums saturated, PSG rendered; merged: composites
   mixed and banked;
6. every channel written (`ChannelWriter`);
7. row 0: leading rests' `C00`, then BPM / speed `Fxx`; mid-song tempo `Fxx` (`ModLayout`).

**Then the layout** (`convert()`), in this fixed order:

```
pattern breaks → loop Bxx (or a stopping song's D00) → trailing patterns trimmed
  → (merged) narrowed to the columns in use → sample names → (compact_slots) slots renumbered
  → one-shots' first word zeroed (samples.pt_zero_bytes)
```

The `Bxx` needs the post-break layout, so breaks always come first.  A merged build with
`merge_bank_slots: auto` may run the whole pass again (up to four builds; § Sample banks).

### MOD limits

| Resource | Limit |
|----------|-------|
| Instruments | 31 |
| Patterns / positions | 127 (`max_patterns`) |
| Rows per pattern | 64 |
| Channels | 4, 8, 10, 12, 14, 16 (format tag) |
| Notes | C1–B3, 36 semitones |
| Effects | one per cell |
| Sample | 131070 bytes in the format, 65534 in the original ProTracker editor; `samples.max_sample_kb` (128 / 64) caps what the generators render, `sample_truncated` warns if one is cut anyway |
| BPM | 32–255 (`Fxx` ≥ $20) |
| Speed | 1–31 MOD ticks per row |

---

## Timing

### Ticks, rows, patterns

A row is `ticks_per_row` duration units.  The parser stores durations already multiplied by the
header's tempo divider, so in stored ticks a row is `ticks_per_row × divider` (`Timeline.ticks_per_row`):

```
row_total = tick / (ticks_per_row × divider)      pattern = row_total // 64      row = row_total % 64
```

- A **note-on** goes on the row it starts in (floor), delayed with `EDx` when it starts between
  rows (§ Notes that start between rows); without a free slot for the delay it is rounded.
- Everything else (rests, cuts, tempo changes, the loop target) rounds to the nearest row
  (`Timeline.pattern_row`).
- `merge_patterns:` pattern numbers count note-ons by floor, shifted past the breaks
  (`Timeline.pattern_of`).

### Choosing `ticks_per_row`

The GCD of the song's note durations puts every note on a row.  A coarser grid still works (notes
between rows take `EDx`) and makes fewer patterns; a finer one wastes rows.  Every tempo segment's
BPM must also fit 32–255 (Drowning needs `ticks_per_row: 2`).

| Common durations | GCD | `ticks_per_row` |
|------------------|-----|-----------------|
| $06, $0C, $18 | 6 | 6 |
| $04, $08, $0C | 4 | 4 |
| $04, $06, $0C | 2 | 2 |

### BPM

The driver reads `fps × (m − 1) / m` ticks a second (tempo modifier *m*; the `TempoWait` mechanism is
`smps_driver.md` § Timing System), and one duration unit is `divider` ticks.  ProTracker plays
`BPM / (2.5 × speed)` rows a second.  Equating rows per second:

```
BPM = fps × (m − 1) × speed × 2.5 / (m × divider × ticks_per_row)        fps = 60 NTSC, 50 PAL
```

| Song | divider | m | speed | tpr | BPM |
|------|---------|---|-------|-----|-----|
| Title Screen | 1 | 5 | 6 | 6 | 120 |
| Green Hill | 1 | 3 | 3 | 2 | 150 |
| Special Stage | 2 | 8 | 6 | 2 | 196.875 → 197 |

`auto_bpm: true` computes it (`derive_bpm`: rounded, clamped to 32–255; 150 when the modifier is
≤ 1); `region` picks the fps.  Otherwise `target_bpm` is used as written.  The speed `Fxx` is
written only when `target_speed` is not 6.

**The BPM is a whole number.**  A rounded BPM drifts against the hardware: Special Stage at speed 3
is 98.4375 → 98, 139 ms behind over its 33 s pass.  `target_speed` changes the MOD ticks per row, not
the row grid, so choose the speed whose BPM is (nearly) whole — speed 6 above.  `convert.py` prints
the error and the better speed; `analyze.py`'s skeleton and minimal configs pick it
(`bpm_rounding_options`).

### Frames versus ticks

`TempoWait` holds every *m*-th V-int frame, so ticks are unevenly spaced: tick *k* of a tempo
segment falls on frame `k + k // (m − 1)` (the song's schedule, `SmpsSong.tempo_schedule`: the driver's
phase, a segment starting the tick after its `smpsSetTempoMod`; `Timeline.holds_before` places `EDx`
by it).  Note fill and modulation count **frames**, not ticks (`smps_driver.md`).  Every frame count is put on the tick timeline with
`Timeline.ticks_per_frame_at(tick)` = `(m − 1) / m` for the modifier in force at that tick (1 for SFX),
never multiplied by the tempo divider.

### Mid-song tempo changes (`smpsSetTempoMod`, $EA)

The flag sets every track's modifier and restarts the hold counter.  `Timeline.collect_segments` splits
the song into tempo segments; each change gets `Fxx` with the BPM scaled by the change in tick rate
(`Timeline.bpm_for`) on its row (`ModLayout.tempo_changes`: a spare column first, then any cell with no
effect, then one holding only a `4xy` continuation).  A loop back into another segment gets an `Fxx`
at the loop target too.  Fills, vibrato rates and `EDx` delays use the modifier at their tick.  A
segment whose BPM leaves 32–255 is clamped and warned (`tempo_bpm_range`).

Inherent: the driver's holds fall at the end of each counter cycle, so the MOD is one to two frames
behind after each change (Drowning ends 59 ms late).  The audit tools allow for it.

### Global duration divider (`smpsSetTempoDiv`, $EB)

`cfSetTempoDividerAll` writes every track's divider, and the driver multiplies a duration by it when
the note is read: a note begun before the change keeps its length, and the last write wins against a
track's own `smpsChanTempoDiv`.  `prepare_song` re-times every channel before anything
reads ticks (the carrying channel first; the parser keeps `smpsChanTempoDiv` as an event so each
note's divider is known).  Rows stay ticks: Credits' half-tempo passage has twice the rows at the
same BPM.  Loop labels are not re-timed; no song that uses the flag loops.

### Tempo commands on row 0

The BPM and speed `Fxx` are placed after every channel is converted (`ModLayout.tempo_commands`),
into cells whose effect slot is free: spare columns first, then any column with no row-0 effect;
failing that a leading rest's `C00` gives way (warned `rest_no_slot` when the song loops to row 0).
Written first, a note's own row-0 `Cxx` overwrote them and the song played at the wrong speed.  In a
merged build only columns that hold notes take them first: an `Fxx` on an empty column keeps it from
being narrowed away.

---

## Notes: range, transpose, routing

### SMPS range to MOD range

SMPS notes are bytes $81–$DF: semitone `byte − $81`, C0 to A#7.  A MOD has C1–B3.  A note's MOD
index is its semitone plus transposition, or a `root` anchor; one outside 0–35 is **clamped** and
warned (`clamp_high` / `clamp_low`), never silenced.

| Source notes | Semitones | Transpose to land on C1–B3 |
|--------------|-----------|----------------------------|
| C0–B2 | 0–35 | 0 |
| C3–B5 | 36–71 | −36 (most FM) |
| C4–B6 | 48–83 | −48 |

A channel spanning more than three octaves needs `voice_map` ranges, each with its own instrument.
FM labels are real pitches (`nA4` at offset 0 is 440 Hz); a PSG byte indexes the driver's table,
whose `nC0` is C3 (`psg_synthesis.md`).

### `voice_map` routing (`resolve_note`)

```
key = note − $81 (range_space: source)   or   the chip's pitch (range_space: chip)
                │
                ▼
ranges = channel_instrument_map[channel][voice]  or else  voice_map[voice]
                │
     entry with low ≤ key ≤ high?
     ├── yes: instrument = entry.mod_instrument (or its detune variant)
     │        ├── root set:  MOD note = root + (key − low)                        path fm_root
     │        └── no root:   MOD note = (note − $81) + driver transpose + transpose
     └── no:  the channel's instrument, MOD note as "no root"; a voice with ranges warns map_gap
```

- `driver transpose` = the header's pitch offset plus every `smpsChangeTransposition` so far;
  `transpose` = the channel's YAML value.
- `channel_instrument_map` replaces the voice's `voice_map` list for that channel; it does not fall
  through to it.
- PSG: a multi-entry `psg_voice_map` list dispatches per note on `low`/`high` (that entry stays
  active); `root` with `low` places a tone as above, `root` alone pins a noise note; noise mode routes
  through `psg_map` (`psg_synthesis.md`).
- `smpsDetune` never takes part: it is a FNUM offset, not semitones (§ Detune variants).

### `root`

`root` is **unconditional**: `low` always plays at `root`, whatever the pitch offset, detune or
`smpsChangeTransposition`.  The sample's rendering pitch (`synth_root`) never moves a note: its
difference from the pitch `root` sounds (`synth_shift`) goes into the sample's rate
(`fm_synthesis.md` § Pitch: synth_root, synth_shift, target_rate).

- **Use it** where a channel's key is fixed: the anchor is exact and independent of `transpose`.
  When the entries cover every note of a channel, set `transpose: 0`.
- **Don't** on a channel that changes key with `$E9` mid-song: the same byte must reach different
  notes, and `root` places them all alike.  Use the transpose path (no `root`), or `range_space:
  chip`.  The same holds for a PSG entry with `low` (Labyrinth Zone's PSG1/PSG2 walk with
  `smpsAlterPitch`: rootless, `transpose: -12` on the channels).
- **Choosing it:** keep `root + (high − low)` within C1–B3, and prefer the highest such `root`: the
  sample plays at `root`'s rate, so a higher `root` keeps more treble (C2 ≈ 8.3 kHz, C3 ≈ 16.6 kHz).

### `range_space: chip`

By default ranges match the source byte.  With `range_space: chip` (song level) they match the pitch
the chip plays — byte + pitch offset + accumulated `$E9`, a PSG note through the driver's table
(`psg_index_semitone`, which also gives the hardware's pitch for notes transposed past the table's
ends) — so `low`/`high` are chip pitches, `root` always sounds `low` (never `synth_root_ambiguous`),
and a voice spanning more than three octaves gets one entry per window.  Two voices sharing one sample keep
separate entries: `root_e = root_head + (low_e − low_head)`.  Needed where a song changes key while
keeping a voice: Credits' FM2 does it twenty times, and matched on source bytes 8 % of its notes
were right.

`tools/config_to_chip_space.py <config>` converts a source-space config: for every entry it collects
the chip pitches each channel plays through it (loops extended, every transposition), writes one
entry per range touched with `synth_root` = its low note and `root` shifted to keep the tuning, and
warns where a range cannot fit C1–B3 at that tuning (those notes need an instrument of their own:
Ending's PSG2).

---

## Effects

### SMPS → MOD mapping

| SMPS | Byte | MOD | Rule |
|------|------|-----|------|
| `smpsAlterVol` | $E6 | `Cxx` | FM TL offset, 0.75 dB/step; `Cxx` only where a note's level differs from its instrument's baked level (§ Levels) |
| `smpsPSGAlterVol` | $EC | `Cxx` | PSG attenuation, 2 dB/step; same rule |
| `smpsPan` | $E0 | (level) | MOD pan is per channel; a hard-panned FM note counts `fm_pan_law_db` (3 dB) quieter |
| `smpsModSet` / `smpsModOn` / `smpsModOff` | $F0 / $F1 / $F4 | `4xy`, a slow one `1xx` / `2xx` / `E1x` / `E2x` / — | § Vibrato |
| `smpsNoteFill` | $E8 | `ECx` / `C00` / release slide | § Note fill |
| `smpsNoAttack` | $E7 | note-on or `3FF` | § Legato |
| `smpsDetune` / `smpsAlterNote` | $E1 | (sample), `E1x` / `E2x` | § Detune variants |
| Streets of Rage's `$F7` (FM3) / `$FC` | | (sample) | channel 3's special mode, the hardware LFO: voice copies (§ Detune variants, § Hardware LFO) |
| `smpsChangeTransposition` | $E9 | (placement) | adds to the driver transpose |
| `smpsSetvoice` | $EF | (routing) | picks the `voice_map` list |
| `smpsPSGform` / `smpsPSGvoice` | $F3 / $F5 | (routing) | `psg_map` / `psg_voice_map` (`psg_synthesis.md`) |
| `smpsSetTempoMod` | $EA | `Fxx` | § Mid-song tempo changes |
| `smpsSetTempoDiv` / `smpsChanTempoDiv` | $EB / $E5 | (re-timing) | § Global duration divider |
| `smpsJump` | $F6 | `Bxx` (+ `Dxx`) | the song's loop (§ Song start, loop and end); a short loop is replayed |
| `smpsStop`, `smpsFade`, `smpsStopSpecial` | $F2, $E4, $EE | key-off, `D00` | § A song that stops |
| `smpsLoop` / `smpsCall` | $F7 / $F8 | — | unrolled / inlined by the parser (`smps_format.md`) |
| `smpsNop`, $ED, `smpsMaxRelRate` | $E2, $ED, $F9 | — | ignored |
| DAC note | | the drum | `dac_samples`: its instrument at its `mod_note` |

### Effect priority

A cell has one effect.  On a note's rows:

1. **Attack row:** `EDx`, `3FF` and `9xx` take the slot; a `Cxx` due there moves to the first later
   row of the note with a free slot (one row at the instrument's own level costs less than 33 ms of
   timing).  Otherwise `Cxx` > `4xy` > an in-row `ECx`, except that an attack-row `ECx` displaces
   `4xy` (a note that short has no audible vibrato).  A cut inside an attack row that needs `Cxx`
   moves to the next row's start (`C00`).
2. **Later rows:** `Cxx` (moved) > `4xy` > `ECx` / `C00` (fill) > a sliding loop's fall (`A0y` /
   `EBx`, or `6xy` on a row whose `4xy` the note set earlier).
3. **After the note:** the release slide (`A0y` rows) takes only free slots and stops at the next
   note-on's row.

A volume change on a vibrato row drops that row's vibrato; `smpsAlterVol` is sparse, so this is rarely
heard.

### Notes that start between rows (`EDx`)

A note whose tick is off the row grid goes on the row it starts in, delayed with `EDx`
(`Cells.note_cell`, `core/convert/channel_writer/cells.py`), instead of being rounded half a row early or late.

- **The delay is measured in frames.**  Ticks are unevenly spaced (§ Frames versus ticks): on Green
  Hill (*m* = 3, two ticks a row) an odd tick is one frame (16.7 ms) after its row, not the 25 ms an
  average tick lasts — exactly `ED1` at speed 3.
  `x = round(frames × target_speed × ticks_per_frame / ticks_per_row)`.
- **It needs the slot.**  A cut (fill, or a PSG note's end) inside the attack row keeps it, and the
  note is rounded instead.  A due `Cxx` gives way when the note lasts two rows or more (it moves to a
  later row); an attack-row `4xy` gives way (later rows carry the vibrato); a rest's `C00` there is
  overwritten (the new note ends the old one).
- **Two note-ons never share a cell.**  When the row already holds the channel's previous note-on (a
  one-tick grace note), the later one takes the next row, undelayed.
- DAC notes carry no other effect, so they always get their delay (unless a `9xx` takes the slot).

### Note fill (`smpsNoteFill`)

The fill counts frames; on the tick timeline it is `fill × ticks_per_frame`.  The cut is placed to the
MOD tick: `ECx` inside a row, `C00` on a row boundary (`ECx` cannot reach past its own row).  Nothing is
written when the fill outlasts the note (`fill × ticks_per_frame ≥ duration`: the duration expires
first) or lands on the next event's row.  A fill equal to the duration byte **does** fire when *m* > 1
(the note lasts `duration × m/(m−1)` frames).  With release slides on, a fill on an FM note starts the
release slide instead of a cut (its sub-row position is given up).  PSG notes with no fill are cut at
the end of their ring (after `smpsNoAttack` continuations: `_ring_ticks`).

### Vibrato (`smpsModSet` → `4xy`)

The driver (`smps_driver.md` § smpsModSet) has a steady cycle of `2 · speed · (steps + 1)` frames (a
step at each turn adds nothing; a driver whose turn moves too, Streets of Rage's, `2 · speed · steps`:
`TrackRules.modulation_turn_pause`) and a swing of `delta · steps / 2` units (a PSG that adds the sum
`>> word_shift`: that many fewer) of the note's own frequency word — the YM2612 FNUM of its pitch class
in the song's table (Sonic 1's: 644 for C … 1148 for A#, B 606 in the block above: a B swings as wide
as a C) or the PSG divider — so the same `smpsModSet` is deeper in cents on C than on A#.  ProTracker advances the vibrato by `x` on each of a row's `speed − 1` ticks and wraps at 64.
`VibratoSpeed.speed` / `vibrato_depth`:

```
x     = 64 · ticks_per_row / ((target_speed − 1) · cycle_frames · ticks_per_frame_at(tick))
swing = period · (delta · steps / 2) / frequency_word         (periods, per note)
y     = the depth whose peak in the player is nearest the swing (_VIBRATO_PEAK)
```

- The peak depends on the player (settings.yaml `player`, `convert.py --player`): PT2 peaks at
  `2y − 1` whole periods, FT2 at `2y − ¼`.
- A swing under 0.7 periods writes no vibrato (the smallest depth would overshoot threefold).  An `x`
  past 15 plays at 15 and is reported.
- A row carries `4xy` when modulation runs for at least half of it, the attack row included, from the
  `smpsModSet` wait (frames) on; the continuation stops at the release slide.
- A per-entry `vibrato: XY` override wins; no shipped config needs one.
- **A cycle too slow for `4x1`** (rounds to `x` 0: Streets of Rage's 251 steps, Moonwalker's 255) is a
  sweep the note never sees turn, not a vibrato: `4x1` would wobble it ±the whole swing many times too
  fast.  Each row with a free slot slides instead to the chip's pitch at its end
  (`modulation_offset`: the wait, `delta` every `speed` frames, a turn after half the steps):
  `1xx` / `2xx` on the row's later ticks, `E1x` / `E2x` under a period a tick; what the MOD reached is
  carried, so a taken row is made up on the next.  The modulation runs from the attack or the
  `smpsModSet` / `smpsModOn` after it (a tie runs it on: Dilapidated Town's FM2 chains), and a tie
  re-struck for its level (`legato: retrigger`) slides back from the note's period on its next row.
- Region-independent (both clocks scale with fps).  What remains is the 4-bit grid: one step of `x`
  is 0.4–0.6 Hz, one step of `y` 10–30 c.

### Legato (`smpsNoAttack` before a note)

The driver writes the new frequency and skips the key-on: the envelope carries on at the new pitch
(Green Hill's one-tick grace notes bending into chords; Drowning's FM3 slide line).  A MOD note-on
re-triggers its sample.  settings.yaml `legato` chooses how such a note is written:

| `legato` | Writes |
|----------|--------|
| `retrigger` (default) | every no-attack note as a plain note-on |
| `strict` | `3FF` (full-speed tone portamento, no re-trigger), with the rules below |
| `loose` | `3FF` on the target's own instrument, always |

`retrigger` trades a grace note heard as two attacks for never riding a decayed tail: Green Hill
FM1 holds an E for 2.8 s, then a no-attack C — the hardware plays it at a fresh note's level, a
`3FF` there is 26 dB down.

`strict`, because in FT2 clone and ProTracker a portamento never changes the sample:

- a target in **another range** of the voice (another instrument, rendered for another octave) is
  written on the sounding instrument, its MOD note moved by the chip-pitch difference (a target
  that would leave C1–B3 is re-triggered on its own instrument);
- after an **`smpsSetvoice`** the note is re-triggered (the hardware rewrites the operators under the
  running envelope);
- with **nothing sounding** on the channel yet it is a note-on (Drowning's FM3 is no-attack from its
  first note).

`loose` sounds right only in a player that swaps the sample on an instrument number (OpenMPT).  A
`3FF` takes the attack row's slot: no `EDx` (the note is rounded), a due `Cxx` moves a row.  Under
`strict` / `loose` the sustain need counts a legato note as the same ring, and `tools/mod_lint.py` flags
a `3xx` with nothing playable under it.

---

## Levels

### FM (`fm_volume_scaling: baked`)

On the chip a note's level is its TL offset — `smpsHeaderFM` volume plus every `smpsAlterVol`, 0.75 dB
per step — and its pan: hard-panned is −3 dB (`fm_pan_law_db`; L/R power, a centred channel drives both
speakers).  A MOD note plays at its instrument's volume unless `Cxx` overrides it, and a second copy of
a sample at another volume costs its full size.  So:

- `LevelPlanner.levels("FM")` walks every FM channel with the conversion's `DriverState` and counts,
  per MOD instrument, notes per level `−0.75 × TL − pan`.  The most common level (ties: the louder) is
  the instrument's **baked level**; its `sample_list` volume stands for it and those notes get no
  command.
- Any other note gets `Cxx = volume × 10^(ΔdB / 20)` (× the channel's `volume / 64`, clamped to 64).
- The sample is **rendered at** that TL offset (`LevelPlanner.fm_render_levels`), so the chip clips a
  multi-carrier voice as the hardware does at that level (`fm_synthesis.md` § Level: render level and the channel accumulator).
- A detune variant votes as its instrument; a merged build's unison gain is part of the level.
- The laws are `core/chips/` (`fm_level_db`, `psg_level_db`); dB → MOD volume is `core/mod/volume.py`.

With the law right, every channel sharing an instrument shows the **same** error against the VGZ, which
one `sample_list` volume fixes (§ Per-instrument levels).  If channels disagree, it is not a volume
problem.

Legacy modes: `true` (header TL as an absolute volume, `Cxx` on every FM note) and `false` (one
`smpsAlterVol` step = one linear MOD volume unit, ≈ 0.3 dB).

### PSG (`psg_volume_scaling: baked`)

The same scheme on the SN76489: level = −2 dB × attenuation (`smpsHeaderPSG` volume +
`smpsPSGAlterVol`, 15 = silent; a silent note neither votes nor sounds), no pan term, planned by
`LevelPlanner.levels("PSG")` with the conversion's PSG instrument tracking.  Most PSG notes then carry
no `Cxx`, which leaves the slot to vibrato and in-row cuts.  Legacy mode `absolute`: `Cxx` on nearly
every PSG note.

---

## Song start, loop and end

### Loop extension

A channel whose data ends in a short `smpsJump` loop (typically PSG3's hi-hat) has the loop body
replayed to the song's last tick (`prepare_song`).  The body is the events **after the jump
label** (`SmpsChannel.loop_event_index`), not every event at the label's tick: Spring Yard PSG3's
`smpsPSGAlterVol $FF` just before its label would otherwise repeat each pass and walk the hi-hat to
full volume.

Tracks that loop at other lengths are in step again only after their periods' least common
multiple (Streets of Rage $8F: 2304, 1728 and 4608 frames, 13824), so every track is replayed to
the last jump target plus that period and the MOD loops back to the target.  A loop's period is
the shortest its body repeats at (Green Hill's drums: 1024 ticks, a 512-tick bar twice); one
under a quarter of the longest is a texture and sets none (its hi-hat).  A period past four
times the longest loop is not unrolled: the song ends as before and `loop_drift` names the
tracks out of step after the MOD's loop (Stealthy Steps' PSG3, 5173 against 5120).

### Leading rests

The drum track's rests write nothing: the sample plays out, as on the chip, and an FM drum rings on
(Type 0 FM); only a silent drum's hit stops it (`ChannelWriter._stop_ringing`).  A driver whose rest
stops the sample says so (`TrackRules.rest_cuts`: Streets of Rage's rests and gates play its empty
`$85`), and its rests write `C00`.

A channel whose first event is a rest gets `C00` at pattern 0 row 0 (`ModLayout.leading_rests`, after
every channel is converted): a song that loops to position 0 otherwise rings its last note through the
rest.  An effect already in that cell moves to a free row-0 cell; with none free the `C00` is
dropped, warned (`rest_no_slot`) only when the loop does return to row 0.  The row-0 `Fxx` are placed
after this (§ Tempo commands on row 0).

### The loop

`ModLayout.loop_point`, after the pattern breaks:

- **Where:** the row of the song's end tick (`round(end / tpr) − 1`, shifted past the breaks), on the
  first column in use with a free effect slot (`loop_no_slot` warns where none is).
- **Target:** `round(loop_target_tick / tpr)` shifted past the breaks → `Bxx` to its pattern.
- **Mid-pattern target:** a `Dxx` (BCD row) on a free column **to the right** of the `Bxx`: ProTracker
  reads a row left to right, and a `Bxx` after a `Dxx` resets the row to 0.
- Patterns after the loop's are trimmed.

### A song that stops

`smpsStop` (and `smpsFade`, `smpsStopSpecial`) keys the track off.  `ChannelWriter._on_stop` ends what
an FM channel still sounds there as a rest would (release slide, else `C00`); a looping channel never
stops, the DAC plays out and a PSG note is already cut.  `ModLayout.song_end` puts `D00` on that row
of a song that does not loop (none on a pattern's last row) and the patterns after go, so the module
restarts after the key-offs, not after a pattern of silence.

---

## Detune variants

`smpsDetune` / `smpsAlterNote` ($E1) adds a raw offset to the frequency word (`FMUpdateFreq`), so its
interval depends on the note: `$03` is +8 c on C (FNUM 644), +4.5 c on A# (1148).  It is not semitones
and never affects range lookup.  A MOD retunes only a whole sample in 12.5 c finetune steps, so
`core/plan/detune.py` (settings.yaml `fm_synthesis.detune_variants`, on by default; FM only) renders
every detune an instrument plays into a sample of its own:

- the detune most of its notes play at is its **own**: its slot's sample is rendered with it (Title
  Screen's FM5 double takes no extra slot);
- every other detune is a **variant** in a free slot, the most played first, sharing the instrument's
  entry, level and `sample_list` volume / finetune;
- one with no free slot plays the instrument's own sample (`detune_no_slot`: Credits);
- a sample carries its detune's interval at the pitch it is rendered at.  Where that is more than a
  finetune step off its notes' (Streets of Rage's +195: +336 c on F#, +266 c on A#), a variant is
  rendered in its notes' commonest pitch class (the same `root`, another `synth_shift`), and notes the
  detune moves more than 25 c from their sample's interval get a variant per pitch class;
- `resolve_note` routes a note to its variant, so every pass sees it.  The plan is made on the song as
  parsed, before the loop extension, as the audit tools make it (`prepare_instruments`);
- a **tie** after a detune change (Scrap Brain FM4's scoop: `smpsAlterNote $EC`, a note,
  `smpsAlterNote $00`, `smpsNoAttack`, duration) is re-written by the driver at the new detune; the MOD
  note keeps its sample, so the tie's row gets `E1x` / `E2x` by the period difference (only a row of its
  own with a free slot; `--verbose` counts them);
- a merged chip composite renders each layer at its own track's detune;
- **channel 3's special mode** (Streets of Rage's `$F7`: each operator at the note's word plus its own
  offset) is a copy of the voice that carries the offsets (`SmpsVoice.fnum_offsets`, made in the walk as
  `$FA` patches are), rendered on channel 3 with `$27` = `$40`.  Its offsets count in the intervals above
  (OP4's, the channel's own A2 / A6: what a rip reads as its pitch), so its notes get a variant per pitch
  class at the instrument's own detune too (Moon Beach's FM3 drums: OP4 +100 FNUM is +254 c on C, +193 c
  on F).

Never stand in for a detune with `finetune: 1`: that moves every note of the slot, detuned or not.

### Hardware LFO

The YM2612 has one LFO: `$22` sets its frequency for every channel, a channel's B4 how far it moves
that channel (FMS its pitch, AMS the level of operators with AM on).  Streets of Rage's `$FC f p a`
writes both, so a note plays at the frequency the last `$FC` of any track wrote (`$88`: FM5's
`$FC 2 3 2` slows FM2's and FM4's vibrato; `$8B`: FM4's `$FC 0 0 0` drops FM5's to 3.8 Hz).
`core/smps/lfo.py`, a pass over the walked song, gives each attacking FM note a copy of its voice under
its LFO (`SmpsVoice.lfo`); the sample is rendered with it (B4 and `$22`).  A MOD sample cannot follow
the chip's free-running phase: under AMS the render starts a quarter cycle in, the level swing at its
middle (as a note starting anywhere hears it on average; at step 0 it is at its quietest), under FMS
alone at step 0, the pitch at its centre.  Its sustain loop spans whole LFO cycles
(`core/audio/loops.py`, `cycle`).

---

## Sample length, sustain loops and release slides

### Sample length versus notes

A sample that does not loop must last as long as the longest note that plays it, measured in the MOD's
own time at the sample's playback rate.  `sustain_duration: auto` computes that per instrument
(`SustainPlanner`, 10 s cap, then each generator's cap to `samples.max_sample_kb`), and `sustain_short`
warns where a note still outlasts its sample.  The rules are `fm_synthesis.md` §
Length: `sustain_duration: auto`.  A looped sample holds any note and warns nothing.

### Sustain loops (`samples.sustain_loops`, `core/audio/loops.py`)

`off` | `merged` (the code's default: the `--merged` build only) | `all` (settings.yaml ships `all`).
An instrument whose envelope settles is cut where it settles plus one loop, so its length no longer
depends on the notes.  `find_sustain_loop`:

1. **Reference:** the RMS envelope (windows of two fundamental periods, dB below the peak) over the
   last second (`SPAN_SECS`) of the instrument's longest note, never the attack.
2. **Flat point:** the first window after which every window stays within `loop_drift_db` (default 1 dB)
   of the reference span's end level, widened by the span's swing around its trend.  A band, not a
   level, so a beating chorus pair is flat once the beat is steady; detrended, so a decaying voice is
   not flat across its decay.  The span's own windows are checked too.
3. **Timbre:** a loop starts only where the harmonic profile (harmonics 1–8, `PROFILE_PER_DB` 0.25 per dB
   of drift) holds until the longest ring ends: Spring Yard's $05 is level-flat from 20 ms while its
   second harmonic swings 30 dB.  Merged builds skip this unless the song sets `merge_loop_timbre: true`
   (it grows the samples).
4. **Length:** every even length (a MOD loop is in words) from 30 ms to `MAX_LOOP_SECS` (1.2 s: a pair
   beating at 1 Hz needs a whole beat), from up to six starts a period apart, scored by the
   discontinuity at the join (two periods after the start against two after the end) plus
   `LENGTH_PENALTY` per second of loop — and, with the timbre check, of the bytes between the flat
   point and the start.
   Never a whole number of cycles: every Sonic voice detunes its operators (DT1), so the best length is
   where their phases come closest to recurring.
5. **Close:** `apply_loop` crossfades the loop's last 15 ms into the samples before its start and cuts
   the sample at its end.

No loop where the reference has decayed below −50 dB (percussive), where the loop would end later than
the plain render (`MAX_END_FRACTION`: still settling), or where the raw discontinuity exceeds
`MAX_ERROR` (≈ −2.5 dB, uncorrelated).  The generators search a 4 s probe render (`PROBE_SECS`) and fall
back to the plain one.  PSG tones loop the same way; noise never loops.

`loop_drift_db` is the size/fidelity knob (settings, song or entry level): at 1 dB a slowly decaying
voice loops only near the end of its longest note and keeps its decay; at 3–12 dB it loops earlier,
smaller, and its long notes end louder than the hardware's.

Per entry (or merge group): `loop_min_ms` sets the shortest loop (an early short loop buzzes);
`loop_start_ms` the earliest start — a detuned pair's swing spans the band from its first window, so
its loop may start in the attack (1-Up's lead pair looped at 21 ms and replayed the onset on every
beat); `loop_decay: slide` below.

### Release slides

A looped sample rings until something stops it, so with loops on every FM note ends with a volume
slide at the voice's release rate instead of `C00` (`Fades.release`): `release_rate_db_s`
fits the dB/s slope of the render's tail after key-off, and one `A0y` per row from the rest's (or the
fill's) row takes the volume to where that slope is at the row's end — the chip's release is linear in
dB, so each row's target is the last one's times a fixed ratio.  Rows whose share rounds to nothing are
skipped, a taken slot is skipped, the slide stops at the next note-on's row, and after 64 rows a `C00`
ends what is left (release rate 0 rings forever).  A release that falls 30 dB within a row (RR $0F:
every Title Screen voice) stays a cut; PSG notes keep their cuts (attenuation 15 is instant).  On a
column another channel's notes borrow, the end is a plain `C00`.  `_clear_stale_cut` removes a rest's
`C00` from a cell a later note-on rounds onto (a note-on with `C00` is silent).

### Sliding loops (`loop_decay: slide`)

A voice whose level falls for as long as it holds (an FM bass on a non-zero D2R) never settles: the
default `freeze` loops it near the end of its longest note, or at a large drift holds it too loud.
With `slide` (entry or merge group) `find_sustain_loop` follows the reference span's trend line: flat is
every window back from the span within `loop_drift_db` of that line extended, the slope refitted over
them, the timbre check always on; a fall under 0.1 dB/s (`MIN_DECAY_DB_S`) is a plain loop.  The render
is flattened from the flat point (`flatten`), the loop found and closed in it, and the fall
(`SustainLoop.decay_db`, `flat_at`) goes to the converter.

`Fades.decay` writes the fall into each note: on every row it rings through after the
attack row, a slide toward `volume × 10^(−fall × (t − t0) / 20)`, with *t0* and the fall at the rate
the note's period plays the sample (a note above the root falls faster, as the unlooped sample did).
`A0y` where the row's share is at least one `A01` (`speed − 1` units), else `EBx` (Game Over's bass falls
a unit a row at speed 9: `A01` every eighth row was a staircase); `6xy` on a row carrying the note's
`4xy`.  Targets are absolute, so a taken row is made up on the next; a `Cxx` row resets the tracked
volume.  The slides stop at the ring's end (its rest's release starts from the fallen volume), the
next note-on or the fill.  Not handled: a sliding instrument as a pcm mix source (the mix has no
slides), a `3FF` note (its curve restarts), PSG.

---

## Pattern breaks (`mod_pattern_breaks`)

A short intro followed by the loop body would share pattern 0 with the body, wasting the pattern's
remaining rows on every pass.  A break at `(pattern P, row R)` ends P at row R with `Bxx → P+1` and
repacks everything after it into full patterns from P+1 (`apply_pattern_breaks`):

```yaml
mod_pattern_breaks:
  - pattern: 0
    row: 31      # last intro row; the body starts at P+1 row 0
```

Every pre-break flat row at or after `P × 64 + R + 1` moves forward by `63 − R` rows
(`shift_for_breaks`; several breaks apply in order).  Green Hill, break (0, 31): flat row 288 →
320 = pattern 5 row 0 (`B05`); flat row 287 → pattern 4 row 63 (`B04` + `D63`).

The loop's `Bxx` is written after the breaks (§ The loop), in post-break coordinates; a `Bxx` written
before would land on a displaced row.

---

## The merged build

The reference MOD keeps every SMPS channel.  The Amiga build (`convert.py --merged`, `core/merge/`)
folds channels together so the song fits three or four: a **group** names a **primary** and its
**followers**; the followers leave the output and the primary plays a **composite instrument**
wherever a follower sounds with it.  Nothing else in the config changes, and the plain conversion is
unaffected: the reference MOD, its baselines and audits stay the ground truth.

### Groups (`merge:`)

```yaml
merge:
  - primary: FM1
    followers: [FM5]        # the lead's detuned double
  - primary: DAC
    followers: [PSG3]       # the hi-hat lands on the drum hits
    cut_primary: true
```

The live channels are packed onto MOD columns 0..n-1 in their configured order (`num_mod_channels`
pads).  Per primary note-on at tick *t*, with the follower's notes as `walk_channel` resolves them
(`smpsNoAttack` continuations extend a note, a rest ends it; a drum or noise note *sounds* for its
sample when that is shorter, and for its fill — `NoteOn.sounding`):

| Follower | Result | Counted |
|----------|--------|---------|
| note-on at *t*, same duration | composite | `paired` |
| note-on at *t*, longer | composite; its tail is cut by the primary's next rest (`truncated`) or re-attacked by its next note (`held`) | `paired` |
| note-on at *t*, shorter | composite, the follower keyed off at its duration inside it (`keyoff_secs`) — unless it ends within `merge_tolerance` of the primary | `shorter` |
| none, resting | the primary alone | `alone` |
| none, still sounding | the primary alone; the ring is lost | `held` |
| note-on while the primary sounds, at no primary note-on | lost; with `cut_primary: true` it plays as a solo note and cuts the primary's tail | `orphan` / `cuts` |
| note-on while the primary is silent | the follower's own note spliced onto the primary's column (its instrument, note, level, fill); its rest follows unless the primary takes the column back | `solo` (`solo_cut`) |

- `merge_tolerance` (ticks, default 1): a follower note-on that close to the primary's counts as at
  *t* (`match_onsets`, nearest first), and a note that short followed by an `smpsNoAttack` note is a
  grace note: the two are one note at the target pitch, so chords fold on the pitches they land on
  (Green Hill's FM3 starts its chord tone a tick after FM4/FM5).
- The **primary's** effects apply to the composite: its vibrato, fill, `Cxx`, `EDx`.  A follower whose
  modulation differs is counted (`vibrato`) but plays the primary's; a solo note has no vibrato.  The
  primary's fill cuts the whole composite (choose the primary accordingly: § Choosing the primary).
- `cut_primary: true` lets a follower note that starts over the primary's tail play and cut it — what
  a hi-hat does to a drum's decay on a 4-channel Amiga.
- `max_composites: N` keeps a group's N most-played composites; the rest play the primary alone.
- `merge_drop: [...]` leaves channels out of the merged build altogether: which parts to keep is a
  musical choice (Green Hill keeps drums, bass, lead and one harmony).
- Followers and dropped channels **stay in every walk** (levels, envelopes, rendering pitches, which
  the composites and solo notes are made from), but not in the output.  Instruments no note of the
  merged build plays are not rendered (`MergePlan.unused`).
- Two channels that never sound at once are a clean pair of solo notes: they simply share a column.

**Survey first.** `tools/merge_survey.py <config>` counts the table above for every ordered pair of a
song's channels and suggests groups (never one with orphans); the converter reports the same counts
for the groups it was given.

### The fill pool (`merge_fill`)

`merge_fill: [PSG1, PSG2]` pools every note of those channels; a group's `fill_lost: true` pools the
follower notes it cannot fold (orphans), and `fill_cut: true` the follower notes whose ring the fold
would cut (a note longer than the primary's, with the primary's next note-on or rest inside it).  The
pool (`pool_notes`) runs before anything folds — a pooled follower note leaves its group and the groups
are paired again — and places each note on **any** output column silent when it starts:

- each column's occupancy is its own notes' sounding spans plus everything spliced onto it;
- the column that stays silent longest wins — the whole note where one can, else one whose next
  note-on cuts it — never for less than a row (`fill_min_ticks`); a note with no silent column is lost;
- a `fill_cut` note moves only where a column is silent for all of it, else it stays folded;
- `merge_fill_cut_after: {DAC: 2, FM2: 4}` lets a column's notes count for that many ticks only, so a
  pool note may cut a kick's decay or a bass note's second half (a column not named is never cut);
- pool notes are spliced as solo notes, keeping their own instrument, level and pitch on whatever
  column they land.

The report gives, per source, notes placed where, cut, lost and stayed folded.  A line that starts on
the other parts' note-ons cannot be pooled: Green Hill's chimes start on a bass and drum hit almost
every time (30 of 188 placed), so they fold onto the bass as bass+chime mixes instead.

### Composite instruments

One composite per distinct **key**; the key holds intervals, not notes, so the same chord shape at
another pitch plays the same composite.

**Chip composites** — every follower an FM voice paired with an FM primary: key `("fm", primary
instrument, (voice, semitones, detune, TL, fill, sides)...)`, an `FmInstrument` with one `FmLayer` per
voice in the instrument catalogue, rendered by `core.synth.fm_render.render_layers`: each layer on its own
YM2612 channel at the composite's rendering pitch plus its interval, its track's detune added to the
frequency word as `FMUpdateFreq` does, its carrier TL the follower's track level relative to the
primary's (a hard pan counts 4 steps), a follower keyed off at its fill (`FmLayer.keyoff_secs`).  The
chip sums and clips the voices as the hardware does.

- Rendered at the level the composite's own notes play most (`fm_render_levels` counts it as any
  instrument).
- Volume: the primary's `sample_list` volume × the composite's peak over its primary layer's alone ×
  the **speaker gain** (layers on opposite speakers are summed in the mono render but never meet on
  the hardware: `_speaker_gain` scales to L/R power; 1-Up's FM3 left + FM5 right read 2.4 dB loud
  without it), moved by the difference between the composite's baked level and the primary
  instrument's **in the reference build** (the level its volume was measured for).  Past 64 it is
  clamped and `merge_headroom` says by how much.

**Mixed (pcm) composites** — anything else: key `("pcm", primary instrument, (instrument, interval,
level scale, fill)...)`, mixed by `mix_pcm_composites` once every sample is in:

- **Trigger note:** the fastest layer's (`_mix_note`: a hat on a kick keeps its treble), capped by the
  group's `mix_note` (Green Hill's drum mixes at F2: 2.5× fewer bytes, no hat above 5.5 kHz), or the
  primary's own with `mix_at: primary` (a looped primary keeps its loop; the followers are resampled
  down into it).  A shape transposed off C1–B3 from where its mix was made gets a mix of its own.
- **Sources** are the generators' unquantised renders (`raw_out`; a drum off disk as bytes unless
  saturated), so a mix is quantised once — by the mixer, or by its bank.  Each layer is resampled by the
  period ratio of its note to the trigger note (a layer resampled *up* uses a 12-tap kernel,
  `UPSAMPLE_TAPS`: the 32-tap sinc rings ahead of a transient and the drum came in late behind the hat)
  and added at `sample_list volume × 10^((level − baked level) / 20)`.
- A follower's **fill** is part of the key: its layer is cut there and decays at the voice's release
  rate (`_cut_layer`; a 2 ms fade where there is none, a PSG note).
- **Length:** the composite's own longest note plus the release padding (`Composite.longest`; padding
  for an FM primary, whose note ends in a release slide, 50 ms for a PSG or drum primary, whose note is
  cut); a layer outlasting that is keyed off with its release, never chopped (a hard cut clicked on
  every kick).  A looped follower is unrolled for that length.  A looped primary mixed at its own rate
  keeps its loop, moved past the followers' tails (a later repeat of the loop body is the same seamless
  loop); one whose longest note ends before its loop starts, or mixed at another rate, plays straight
  through.
- **Level:** the sum is peak-normalised and the composite's volume set to the sum's level; past full
  scale it stays at 64 and `merge_headroom` reports it — unless the group's `limit_db` limits the
  peaks by up to that many dB instead (`limit_peaks`: denser, a little transient distortion).
- Group sound options: `treble_shelf_db` / `treble_shelf_hz` (a brightness shelf on the group's
  composites, on top of any song shelf), `dither:`, `name:`.

**`fm_on_chip`** (a group key, on unless `false`): a mix with an FM primary and FM followers renders
those FM voices together on the chip (`Composite.chip_base`, an `FmInstrument` under an id from
`CHIP_BASE_IDS`, never a slot), unlooped, for `Composite.longest_played` (each note's end times its
playback speed over the mix's trigger note), at the composite's own render level; the mixer takes it in
place of the primary's sample, at the primary's volume times `chip_gain`, and mixes only the rest (a
PSG, a drum) on top.  The voices' detune and phase run on as the hardware's do, instead of two looped
samples repeating a few tens of ms each.  The render takes the primary's loop away: `mix_at: primary`
then keeps none, and a long mix loops only where `loop_mix` finds the sum settle.

**`loop_mix: true`** (a group key): a long mix with no loop loops where its sum settles, found in the
finished mix as a voice's sustain loop is, with an 80 ms crossfade (the layers beat; the join lands on
another phase of the beat); the group's `loop_drift_db` / `loop_min_ms` / `loop_start_ms` steer it.
Lossy: the chord's slow movement freezes in the loop.

### Mixes end where no note reaches

Per mixed note the plan records where it ends and where the next note-on of its primary's column (own,
spliced and pooled notes; not `smpsNoAttack` notes) cuts it, as the MOD places them — a note-on on a
row boundary exactly, one between rows half a row either way, two in one row a row apart — and how
much faster than the mix's trigger note it plays (`_Planner._measure_heard`, `Composite.heard`).  The
mixer cuts the finished sum, with a 2 ms fade, at the latest point any note reaches: the earlier of
its end plus the release slide (an FM primary's: until the voice has fallen 48 dB, `RELEASE_FLOOR_DB`,
at most the release padding) and its next note-on.  A kept loop no note reaches is dropped and the mix cut the
same way.  The layer cuts inside the mix are unchanged, so what plays before the cut is the same
audio.

### A follower's note fill

The driver keys a follower off at its `smpsNoteFill` while the primary plays on (Green Hill's bass: a
67 ms pluck under every kick), so the fill is part of every follower key: a chip layer is keyed off
early, a pcm layer cut with its release (above).  A **solo** note keeps its own fill, and a PSG solo note
ends at its duration on whatever column it lands; an FM solo note on a PSG primary rings into its
rest's release.  A solo note spliced onto another chip's column (an FM note on the drum column) counts
toward its own instrument's sustain in its own chip's pass, every other note-on of that column ending its
ring.  The source instruments' sustain needs count the notes they play inside composites,
and skip followers' own walks and a live channel's folded notes.

### Per-pattern folds (`merge_patterns:`)

A song-wide group folds a follower everywhere.  When the arrangement wants different folds per
section (Green Hill: the bass rides the drum column through the verse and has its own in the intro and
bridge), the groups are given per block of patterns:

```yaml
merge_patterns:
  - patterns: "1-4"           # hex, as Fast Tracker shows them: "0", "1-4", "d-10", "0, 5-c"
    drop: [PSG2]              # their notes in these patterns are left out
    groups:
      - primary: DAC
        followers: [FM2, PSG3]
        cut_primary: true
      - primary: FM3
        followers: [FM4, FM5, PSG1]
        mod_channel: FM2      # take FM2's column here (the bass is folded into the drums)
```

- **Pattern numbers** are the reference build's, after its `mod_pattern_breaks`; a note belongs to the
  pattern its note-on lands in.  A YAML integer is decimal; write strings for hex.
- Each group is a `MergeGroup` with a `patterns` set: its primary's and followers' notes are restricted
  to those patterns and paired as before.  A channel may follow in one block, lead in the next and be
  kept in a third.
- A channel **stays in the output** unless it is a follower or dropped in every named pattern and every
  pattern it plays in (or song-wide).  In the patterns it follows in, its notes are `MergePlan.folded`
  and `ChannelWriter` skips them on its own column; the first folded note-on ends whatever of its own
  still rings there (release slide or `C00`), and its rests there write nothing.  The output has one
  column per source that is live anywhere (Green Hill: 8 of 9), each keeping its meaning for a hand
  finish in a tracker.
- A fold lands on its **primary's** column.  `mod_channel:` (a `channels:` number or a source) moves the
  primary's notes to that column in the group's patterns; the column must be free there (its owner
  folded, dropped or moved) — two sources on one column in a pattern is an error naming it.  A group
  with no followers and a `mod_channel` is a plain move.
- `ChannelWriter` routes each note-on by its tick's reference pattern (`ColumnRouter`); rests and cuts
  follow the note to its column, a ring left on another column when the block changes is cut there,
  and a channel's own end in a column another channel borrows is a plain `C00`.
- A group with no followers and `fill: true` pools its primary's notes in the block's patterns (the
  channel has no column there; unplaced notes are lost).  Its `cut_after: N` lets a pooled note take
  that group's column once its note is N ticks old (`MergePlan.cut_after_at`; `merge_fill_cut_after` is
  the song-wide fallback).  Green Hill's bridge arpeggio is sprinkled over the four columns this way.
- Two groups may share a primary in different patterns; identical chords in two blocks share one
  composite (`Composite.uses`).  A song-wide `merge:` may sit beside `merge_patterns:`; a channel claimed
  twice in one pattern is an error.  Notes of a departed channel in patterns no group folds are lost
  (`merge_dropped`); patterns no block names fold nothing (`merge_unspecified`).

### Choosing the primary (`tools/fold_csv.py`)

`tools/fold_csv.py <config> <table.csv> [--write] [--bank] [--mix-note F2]` writes the
`merge_patterns:` section from a fold table: a row per pattern (hex), a column per reference MOD
channel, each cell `fold N` / `fold N*` / `keep` / `drop` / blank (kept where the channel plays, else
dropped); equal rows join into one block.  The table cannot name a fold's primary, so the tool tries
every member:

- the drums own any fold they are in (every note cuts a drum's decay on an Amiga, and the fold stays on
  the drum column);
- otherwise the member that plays the fewest follower notes **wrong**: lost ones, plus paired ones the
  primary's own effects would distort (`distorted`: a primary fill shorter than the follower's note
  cuts the composite; the primary's vibrato is the composite's).  A chime with a 16-frame fill and
  vibrato never leads a chord it rides.

The counts are printed; `fold N*` names the primary by ear over the measurement.  `--write` replaces
the block between its marker comments; the config stays the source of truth.

### Sample banks (`bank: true`)

A group's mixed composites share instrument slots, each sound chosen with `9xx` (offset × 256 bytes, up
to $FF00; `core/merge/banks.py`).  `pack_banks` runs once the mixes exist, most-played first:

- each sound starts on a 256-byte boundary and is followed by one MOD tick of silence (at the slowest
  tempo) up to the next boundary; one finetune per bank;
- a bank plays at its loudest member's volume, quieter members scaled into their bytes (8-bit range
  lost), so `_layout` groups sounds by volume where that takes no more banks than first fit;
- a looped mix goes last in its bank (the bank's loop header is its loop) and its notes need no cut;
- chip composites are never banked;
- a member that fits nowhere is dropped like any composite over budget (§ Slots) and reported
  (`merge_bank_dropped`); banks the slots could not hold are counted (`MergePlan.bank_overflow`).

**In the output** every banked note starts with `9xx` (none at offset 0) and, unless the column's next
note-on comes first, is cut once its sound is over (`Cells.cut_after`: the sound's seconds as
frames, then ticks as a fill is; `C00` on a row, `ECx` inside one).  A melodic primary banks too; the
`9xx` takes the attack row: a `Cxx` moves to the note's next free row, an `EDx` is dropped, a cut inside
the attack row moves to the next row, and a no-attack note is re-triggered.  A banked sound's level is
measured under its own id (`Composite.bank_id`), and its release slide takes its primary's rate.

**`bank_drums: true`** (a drum primary's group; implies `bank`, needs no followers) puts the drums
themselves in the banks: a hit no follower sounds with is a composite of the drum alone (no layers),
banked with the mixes, so no note plays a drum's own slot.  The slot fit then offers those slots after
every other one, so the bank reserve takes them first (a drum's slot becomes a bank) and composites
the rest.  The converter keeps such a drum aside for the mixer (`_mix_sources`: read off disk, or its FM
drum render) instead of installing it.  A bank holding a lone drum is packed before any other: were it
dropped, its hits would fall back to a slot that holds something else now.  Space Harrier II's Harrier
Saga (`configs/space_harrier_2/81_harrier_saga_4ch.yaml`): five drum slots become two banks, and its
FM4 arpeggio rides the drum column as drum + FM4 mixes, so all six channels fit four.

**`merge_bank_slots`** — slots the composite fit holds back for the banks (the banks also take any slot
the fit leaves free).  `auto` (the default): the conversion runs again with the reserve the banks
turned out to need (`bank_reserve_wanted`, up to four builds) — fewer where a held slot sat empty while
composites went without, more where bank sounds carry more notes than the least-played composites they
would displace.  A number pins it, and a held slot left empty goes back to the composites once
(`merge_bank_retry`).

### Slots

Composites share the 31 slots with the instruments the merged build still plays.
`fit_composites`:

- **Offered:** slots nothing in the config names, then those of instruments no note of the merged build
  plays.  A drum's slot is never reused (it is loaded from disk), except a `bank_drums` group's (§ Sample
  banks), offered after every other slot so the bank reserve takes it first.  A mix source's slot can be: its sample
  is rendered anyway and kept aside for the mixer (`MergePlan.mix_only`, `_mix_sources`); an FM source's
  slot takes only a pcm composite (the FM catalogue holds one entry per slot).
- **Order:** each group's `max_composites` first, then the most-played composites first.
- **Over budget:** composites are dropped — same-shape twins first, then those whose primary instrument
  is played anyway (no new slot needed), then the least played — and the fit is redone, since a dropped
  composite hands its notes back to the primary's own instrument and releases its sources.
- **A composite owns its slot:** both catalogues drop the instrument formerly named there.
- The report: `composite slots: N used of M free (K asked for)`; `merge_unsupported` lists the notes that
  play the primary alone.

Three rules keep one sound out of two slots:

- **A unison is the primary, louder** (`unison_gain_db`).  A chord whose every follower is the
  primary's own voice at the same pitch, no detune, keyed off with it (chip), or the primary's
  instrument at its note with no cut (mix), makes no composite: the note plays the primary's
  instrument with the followers' gain (`MergePlan.gains`; amplitudes add on a shared speaker, powers
  across speakers: +6 dB for an equal pair on one side, +3 dB for left + right).  The gain is baked into
  the instrument's level (`ResolvedNote.gain_db`), its `sample_list` volume moved accordingly
  (`merge_unison_volume`); a `Cxx` instead would land a row late on every delayed note.  A detuned
  unison beats, so it stays a composite.  Any instrument whose commonest level moves in the merged
  build has its volume moved the same way (`MergedBuild._bake_moved_levels`, reported as "level
  moved"), so every note keeps the reference build's volume: Space Harrier II's FM5 plays its $4B voice
  mostly in chord composites, and FM4's solo notes, 4.5 dB louder, became the instrument's level.
- **Twins give up their slot first** (`same_shape_twins`).  Composites of one shape (same voices and
  intervals, differing only in fill or level) are twins; the one whose followers ring furthest (`_reach`:
  fewest cut, then the latest cuts), then the most played, is kept, if it can play every note of the
  other (a mix transposed past B3 cannot).
- **Stand-ins** (`stand_in`): a composite dropped for any reason plays a surviving one of its shape
  ("stands in") instead of losing the follower.

`merge_twins: always` (song level; default `short`, only while slots are short) drops twins even when
everything fits, for the bytes.

### Other merged-build rules

- **Rendering pitch:** `merge_max_synth_shift` (default 12) caps how far above its root's pitch a sample
  is rendered; 0 halves every shifted sample, at the cost of envelopes running faster up the range
  (`fm_synthesis.md` § Pitch: synth_root, synth_shift, target_rate).
- **Loops:** `samples.sustain_loops: merged` loops this build only; its loops skip the timbre check
  unless `merge_loop_timbre: true`.
- **Columns:** a merged build whose columns fit four is written as a 4-channel M.K. file
  (`ModFile.narrow_to`); its slots are renumbered from 1 without gaps (`samples.compact_slots: merged`,
  `ModFile.compact_samples`; the cells, warnings and report follow, the sound is unchanged).  Reference
  builds keep the config's numbers, which `sample_list` tuning is keyed on.
- **Output:** `merge_output_file`, default `<output stem>_merged.mod`.

### When it runs

The plan is built once the ticks are final (after `prepare_song`) and before any sample is rendered, so
chip composites are catalogue entries like any instrument.  It lives on `config.merge_plan`, which
`walk_channel` reads: the level pre-passes, the sustain scan and `ChannelWriter` see composites the same
way.

### Verifying a merged build

- **Regression:** every config with a merge section is a second case, `<name>_merged`.
- **Against the VGZ:** `tools/vgm_compare.py <config> <vgz> --merged` renders each merged column and,
  with a combined mute mask, the sum of the chip channels folded onto it, and reports whole-song balance
  and audio onsets per column.  With `merge_patterns:` it renders every chip channel once and builds
  each column's reference per pattern from the channels that sound there (`core.merge.column_sources`):
  a column × pattern-block table of block level and the primary's key-on attacks against the song's
  median block, flagged at 2 dB.  Pooled notes are in no reference.  The per-note audit needs one note
  stream per channel, so it is the reference build's.
- **Samples:** `tools/mod_audit.py <mod> [--banks]` reads any MOD back and reports each sample against
  the notes that play it: unused, too short, oversize, empty slot, low rate, `same as N` (first 100 ms
  correlate ≥ 0.98), `finetune variant of N`, and per bank sound with `--banks`.
- **Size cuts:** `tools/mod_render_diff.py` renders two MODs per channel and reports the worst 20 ms
  difference; hold the dither seed fixed to see past its noise.

---

## Verifying against a VGZ

### Setup

`reference/vgz/` is untracked: it holds the VGZ rips and VGMPlay.

1. Put the rips in `reference/vgz/<game>/`, the folder its configs have under `configs/` (Sonic 1's:
   `reference/vgz/sonic_1/`).
2. Unzip a **VGMPlay 0.51.x** Windows build (<https://github.com/ValleyBell/vgmplay-libvgm>) into
   `reference/vgz/vgmplay/` (`VGMPlay64.exe` or `VGMPlay.exe`, `VGMPlay.ini`, `zlib1.dll`).  The 0.51 line
   is required: the tool patches `VGMPlay.ini` with `Core = NUKE` / `MuteMask = …`, which 0.40.x lays out
   differently.  Looked up as `--vgmplay DIR` → `VGMPLAY_DIR` → `reference/vgz/vgmplay/`.
3. An ffmpeg build with the libopenmpt demuxer (`ffmpeg -h demuxer=libopenmpt`; the gyan.dev *full*
   build has it), and `pip install numpy`.

### Running it

```bash
python convert.py configs/sonic_1/01_title_screen.yaml
python tools/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz"
python tools/vgm_compare.py <cfg> <vgz> --skip-render     # reuse output/compare/<cfg>/*.wav
```

Report sections: per-note pitch/level, per-channel summary, pitch verdict, vibrato, channel balance
(`--ref`, default FM2), per-instrument levels, onset timing, noise (decay, band profile), DAC rate.
Notes the recording plays after the MOD's single pass has ended are left out.  Levels are L/R power,
never a mono mix (a hard-panned YM2612 channel reads ~5 dB low in mono).

### The song before conversion (`tools/vgm_lift.py`)

Does the song as read - asm or ROM bytecode - play what the rip recorded?  No MOD, no audio: the rip
is lifted to a song (`core/vgm/lift/`) and both are compared note by note through `played_song`
(`core/audit/rip_diff.py`).  A difference here is the reader's or the lift's, not the converter's;
one only `vgm_compare` / `vgm_pitch_audit` show is the converter's.

```bash
python tools/vgm_lift.py configs/moonwalker/88_round_clear.yaml       # a config and its rip
python tools/vgm_lift.py --all --configs configs/moonwalker --skip DAC  # every pair, a line each
```

- Pairs: a config and its rip share a number, or the `rips.yaml` beside the configs maps config stem
  to rip (`core/audit/rips.py`; `measure_volumes.py` pairs the same way), and may log the rip's own
  faults (below).  Rips sit in `reference/vgz/` under the configs' subfolder.  `--input FILE [--rom-song ID]` names any song.
- Both sides as shipped (data bugs kept).  The lift takes the song's tempo modifier (where it starts:
  tempo changes are still found) and divider; inferred only where it fits no schedule, said so
  (`--infer-tempo` to judge the inference).  The divider is spelling: durations are ticks on both
  sides (a song's already multiplied by it), so it never moves a note; it sets how the converter counts
  rows (`ticks_per_row` × divider).
- Aspects: by default what the lift reads (`LIFTED_ASPECTS`: onset, length, note; `--aspects all` for
  every one - a lifted note's pitch is its table word, no detune yet).  The lift matches a note to the
  song's own FM table and holds at its phase (`lift_song(frames, song.rules, ...)`).
- A tie that changes nothing compared is one note on both sides: a rip shows a read only where the
  driver writes the frequency on reads alone (Sonic 1); Type 0 FM writes it every frame.
- Channels: those both sides play; `--channels` / `--skip` (prefixes) narrow it, and the ones only one
  side plays are named, not compared.  Ticks print with seconds into the song.
- Verdict per kind, the song's (`channel_type`, not the name): `FM same · DAC onset 4 · PSG note 31
  (lift unfinished)`.  FM is what the lift reads in full (`LIFTED_KINDS`): its verdict is the song's;
  a DAC or PSG difference may be the lift's (vgz_conversion.md 1.6-1.8).  `--all` ends with both
  counts (Sonic: 13 of 19 FM same · 3 on every channel).
- A rip is only as good as its emulator and ripper: a whole song off from one point on is a frame the
  rip lost or gained (a glitch, below), which the lift would read as a tempo change; a rip's
  `rips.yaml` entry has it undone first.

### The rip's own faults (`rips.yaml`, `tools/vgm_frames.py --glitches`)

A rip records its emulator's run, not only the song.  What is the rip's, not the song's, is logged
per rip in the `rips.yaml` beside the configs (`core/audit/rips.py`): the entry, a plain rip name
otherwise, becomes a mapping.  Frames are the rip's (FrameLog indices, as `vgm_analyze.py --frames`
and `--glitches` print them):

```yaml
84_path_of_fiend:
  rip: "07 - Path of Fiend.vgz"
  glitches:                     # a V-int lost (-1) or gained (+1): every channel off from `frame` on
    - {frame: 2304, frames: -1, why: "burst at 2303 (164 writes, every channel re-keyed) runs into 2304: its V-int lost"}
  foreign:                      # another sound on these channels; from / to: frames (default: all)
    - {channels: [FM4], from: 120, to: 900, why: "..."}
```

- Glitch: the driver missed a V-int (or ran twice in one), so every channel keys a frame late (early)
  from there on, for good.  Seen so far (Space Harrier II's stage theme, five times; its staff roll;
  Golden Axe's Path of Fiend): a burst that re-keys every channel and loads their voices runs past
  its frame into the next V-int's time, and that V-int is lost.  `vgm_frames` and `vgm_lift` read the
  log with each glitch undone (`core/vgm/realign.py`: the frame before a lost V-int merged into its
  own, a held frame inserted for a gained one), so the song after it is still checked.
- Foreign: another sound on a channel (a sound effect, a voice left keyed).  `vgm_frames` sets its
  notes aside ("excused: another sound"); `vgm_lift` does not compare a channel held throughout
  ("another sound: FM4") and sets aside the differences on its frames ("known rip faults: FM4 3").
- Two misses every rip shows alike are no glitch: `vgm_frames` excuses them, counted apart
  ("excused: log end 4, re-entry 2").  **Log end**: a note on the log's last frame, which the log
  ends inside, mid-burst.  **Re-entry**: a channel's first note back in its loop keyed a frame late, as
  the next frame has it (the re-entry's burst overruns its frame; this time the next V-int is kept).
- Finding glitches: `python tools/vgm_frames.py --all --glitches --configs configs/<game>` follows each
  FM channel's key-ons (on their frame, at the note's pitch) from where the rip starts, and moves a
  channel when two attacks running land only on another offset.  Moves of one shift whose windows
  overlap are one candidate: a **glitch** when every channel playing across it moved (two at least;
  one silent across several glitches moves by their sum), else the song's (one channel alone, or
  from a channel's first attack).  Each candidate prints its frame, seconds and each moved channel's
  attacks on time before / after; `[rips.yaml]` once logged.  The scan prints, never writes: a
  glitch goes into `rips.yaml` once its writes are read (`vgm_analyze.py --frames` around it).
- `vgm_frames` alone cannot see a glitch: it judges the registers on a note's frame, and a frame late
  they mostly still hold the note.  Path of Fiend "matched" every note at offset -1 with its glitch in
  place; with it undone, at its true offset 0.
- Left out (2026-10-10): Sonic 1's Game Over moves FM1 and FM4 a frame at 6.8 s (frame 406, after 26
  silent frames; 6 attacks after): two channels, too little to log.  Space Harrier II's title (`$98`)
  FM4 is no other sound: its part plays 768 frames later in the rip than the song walks it (99 of 118
  attacks land there), and its OP1 SSG-EG holds `$FF` from before the song.

### Pitch verdict (`tools/vgm_pitch_audit.py`)

The authority on "is every note right", symbolic and self-aligning: the chip's frequency registers
against the pitch each MOD note sounds at, with a per-instrument verdict ("synth_root is 1 octave too
high" — fix that instrument; "mixed" — a note problem).  `vgm_compare` prints it as "Pitch verdict";
run it alone with `--list` for every wrong segment.  The per-note `vgm_c` / `mod_c` columns are audio
cross-checks on the strongest of a note's first four partials (a carrier at multiple 2+ has nothing at
the register frequency); `<-- PITCH` flags the two renders disagreeing by more than 25 c, which is left
to grace notes.

### Per-instrument levels

The table to set `sample_list` volumes from.  Each note is matched to the instrument (and `Cxx`) that
plays it, and the error MOD − VGM is reported per instrument with a per-channel breakdown:

```
inst sample            vol notes    err spread suggest   per channel (Cxx: err xnotes)
  11 ghz_v05_lo.raw     16   116   +4.6    0.2       9   FM3 C1D: +4.8 x20  FM4: +4.6 x58  FM5: +4.6 x58
```

- Only notes without a `Cxx` say what the volume should be: `suggest = volume × 10^(−err/20)`.
- In the baked modes every channel sharing an instrument must show the same error.  `spread` is their
  disagreement; above 3 dB nothing is suggested — look at pan, fills, a wrong instrument, alignment.
- Errors are relative to the song's median note.  The DAC (fixed samples at 64) becomes the anchor only
  when it sits 2 dB or more below that median.

`--write-volumes` writes the suggestions of 1 dB or more into the config, with `# VGZ: +4.6 dB at 16`
on the line; re-convert and re-run to verify.  `tools/measure_volumes.py` does it for every song
(cores − 1 in parallel): convert, one write pass, re-convert, verify, then a line per song with the
volumes changed, what is still ≥ 1 dB off (ceiling, channels disagree, two-note instruments marked) and
the pitch verdict.  Exactly one write pass: the errors are relative to the median, so a second pass
drifts the whole song.  Run it after any change to how samples are rendered.

### Vibrato table

Every FM / PSG-tone note of 0.5 s or more is pitch-tracked in both renders (one partial, heterodyned,
instantaneous frequency).  A row prints when either side modulates: rate and ± cents over the
modulated stretch only.  Flags: `VIBRATO` (rate off by > 15 %, or depth by > 5 c and 30 %), `MISSING
in MOD`, `not in VGM`.  `b` rows are **beating**: two detuned carriers also swing the partial's level at
the rate (≥ 15 %); a `BEAT RATE` flag points at `synth_root` (a beat follows playback speed), never at
`4xy`.  A PSG note starts where the channel becomes audible or its period moves more than 70 c
(`core/vgm/notes.py`); smaller moves are modulation inside the note.

### Onset timing

Each FM / PSG / noise key-on in the register log is matched one to one to a MOD note row within 40 ms
(`EDx` rows at their delayed start), so `unmatched` counts notes the MOD lacks or misplaces and
`MOD-only` counts re-triggers where the hardware ties; unmatched times are listed (JSON `unmatched_s`).
A chip key-on counts as a note only after a key-off or a pitch move of more than 70 c — under
`smpsNoAttack` the driver still writes key-on.  The window follows the running deviation; a change of 20
ms or more from first notes to last prints as `drift` (a MOD off the driver's tempo), and a step of up to
120 ms confirmed by the next two notes re-syncs (a tempo change).  The DAC uses audio onsets (the log
holds PCM seeks, not hits), so its count is approximate.

### CI use

`--json FILE` writes the whole report plus a `checks` list and `passed`.  Thresholds are opt-in; any
failure exits 1:

| Flag | Fails when |
|------|-----------|
| `--fail-balance-db DB` | a channel's level relative to `--ref` differs from the recording by more than DB |
| `--fail-pitch-cents C` | the pitch verdict has a note more than C cents off or missing, or a note is silent in the MOD render |
| `--fail-unmatched N` | a channel has more than N chip key-ons with no MOD note row within 40 ms |
