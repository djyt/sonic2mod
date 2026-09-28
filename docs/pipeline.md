# sonic2mod Conversion Pipeline

How SMPS assembly music maps to Amiga ProTracker MOD format.

Related docs: `docs/smps_driver.md` (driver internals), `docs/smps_format.md` (assembly syntax),
`docs/architecture.md` (module overview), `docs/mod_effects.txt` (ProTracker effect reference),
`docs/fm_synthesis.md` (FM synthesis — synth_root, pitch matching, OPN2 internals),
`docs/psg_synthesis.md` (PSG synthesis — psg_map/psg_voice_map, envelope tables, SN76489 internals),
`docs/yaml_config.md` (full YAML schema).

---

## Data Flow

```
  .asm file
      │
      ▼
  SmpsParser.parse_file()          smps_parser.py
      │  • strips comments, collects labels
      │  • parses header macros (voice ptr, channel headers, tempo)
      │  • walks each channel's dc.b stream: notes, durations, coord flags
      │  • unrolls smpsLoop, inlines smpsCall
      │  • produces SmpsSong IR
      │
      ▼
  SmpsSong (intermediate representation)
      │  • SmpsSongHeader  (tempo, channel counts, voice label)
      │  • SmpsChannel[]   (header + ordered SmpsEvent list)
      │  • SmpsVoice[]     (FM patch parameters, not directly mapped)
      │
      ▼
  SmpsToModConverter.convert()     smps2mod.py
      │  1. Create ModFile (10-channel default)
      │  2. Set BPM + Speed on pattern 0 (Fxx effects)
      │  3. Optionally run generate_fm_samples() → synthesized PCM
      │  4. Load sample files from sample_list (or placeholders)
      │  5. _extend_looping_channels() — extend short PSG loops to match song length
      │  6. For each configured channel: walk SmpsEvent list → write MOD rows
      │  NOTE: _set_loop_point() is NOT called here — see post-processing below
      │
      ▼
  apply_pattern_breaks(mod, breaks)   mod.py   [optional — only if mod_pattern_breaks set]
      │  • Splits pattern stream at (P, break_row); repacks body into patterns P+1..N
      │  • Writes Bxx at (P, break_row) → P+1 (skip blank tail rows of pattern P)
      │  • Remaps any existing Bxx effects (loop jumps must NOT be written before this)
      │
      ▼
  converter._set_loop_point(breaks)   smps2mod.py
      │  • Scans post-break MOD backward for last row with non-zero period
      │  • Maps loop_target_tick → post-break (pattern, row) using break formula
      │  • Writes Bxx (+ optional Dxx companion) at the last data row
      │
      ▼
  ModFile → mod.get_bytes()        mod.py
      │
      ▼
  .mod binary
```

---

## SMPS → MOD Effect Mapping

One effect per note-row in MOD format. See `docs/mod_effects.txt` for full ProTracker effect documentation.

| SMPS Command | Byte | Parameters | MOD Effect | MOD Code | Notes |
|-------------|------|------------|------------|----------|-------|
| `smpsAlterVol` | $E6 | signed TL delta | Set Volume | `Cxx` (Cmd C) | FM: cumulative TL offset, 0.75 dB/step. `Cxx` only on notes whose level differs from the instrument's baked level — see §FM levels |
| `smpsModSet` | $F0 | wait,speed,change,step | Vibrato | `4xy` (Cmd 4) | x from the driver's cycle length, y per note from its swing in cents (gotcha 4); triangle→sine |
| `smpsModOn` | $F1 | — | Vibrato (continues) | `4xy` | Re-activates stored params |
| `smpsModOff` | $F4 | — | (clears vibrato state) | none | No MOD effect; future notes have no vibrato |
| `smpsNoteFill` | $E8 | frames | Note Cut | `ECx` / `C00` | Fill is in V-int **frames**; scaled by `(mod−1)/mod` onto the tick timeline, then placed to the MOD tick: `ECx` inside a row, `C00` on a row boundary. Any fill length works; skipped when it outlasts the note |
| `smpsJump` | $F6 | address | Position Jump | `Bxx` (Cmd B) | Target = post-break pattern for the max loop-start tick; Bxx placed at last row with a note period in the post-break MOD; see §Pattern Breaks |
| `smpsLoop` | $F7 | idx,count,addr | (none — unrolled) | — | Loop body replayed at parse time |
| `smpsCall` | $F8 | address | (none — inlined) | — | Subroutine events spliced into caller |
| `smpsSetvoice` | $EF | voice index | (instrument routing) | — | Updates voice_map lookup; no direct MOD effect |
| `smpsChangeTransposition` | $E9 | signed byte | (pitch shift) | — | Updates total_transpose; affects next note placement |
| `smpsDetune` / `smpsAlterNote` | $E1 | signed byte | (none) | — | FNUM offset (~10 cents); not applied to MOD pitch |
| `smpsPan` | $E0 | direction | (level only) | — | MOD panning is channel-based, but a hard-panned note counts `fm_pan_law_db` (3 dB) quieter than a centred one — see §FM levels |
| `smpsNoAttack` | $E7 | — | (flagged on note) | — | No MOD equivalent; note plays without re-attack in SMPS |
| `smpsNop` | $E2 | byte | (none) | — | Game sync byte; ignored |
| `smpsPSGform` | $F3 | byte | (routing) | — | Looks up `psg_map[byte]` → new PSG instrument |
| `smpsPSGvoice` | $F5 | label | (routing) | — | Looks up `psg_voice_map[label]` → new PSG instrument |
| `smpsMaxRelRate` | $F9 | — | (none) | — | FM1 release; ignored |

### Legato (`smpsNoAttack` before a note byte)

The driver's `cfNoAttack` sets a flag; the next note byte writes its frequency and skips the
key-on, so the envelope carries on at the new pitch.  Sonic 1 uses it for 1-tick grace notes
that bend into a chord (Green Hill's stabs: FM4 and FM5 play `F2` for one tick, then `E2`
legato; FM3 the same a tick later) and for Drowning's FM3 slide line (240 of 241 notes).  A MOD
note-on re-triggers its sample, so `_convert_channel` writes a legato note as the target note
with a **full-speed tone portamento**, `3FF`: the period slides to the new note within a tick
and the sample is not re-triggered (the instrument number only resets the volume).  The
portamento takes the effect slot, so the note gets no `EDx` (it is rounded to the nearer row)
and a `Cxx` due on it moves to the next free row of the note, as a delayed note's does.  The
sustain scan counts a legato note as the same ring.  Before this, every stab was heard twice.

A portamento never changes the sample, so when the target note resolves to **another
instrument** — another range of the same voice, its sample rendered for another octave — the
target is written on the instrument that is sounding: the previous MOD note moved by the
chip-pitch difference, with the previous instrument number.  Green Hill's FM3 grace `C6`
(voice $08's C6–B6 range, instrument 15, rendered an octave up) bends into `B5` (its C5–B5
range, instrument 14): written as `B2 3FF` after `C2` it slid instrument 15's sample up to
B6, an octave high; it is now `B1 3FF`.  A target that would leave the MOD's three octaves
that way is re-triggered on its own instrument instead.  Stage Clear has one such note.

A no-attack note after an `smpsSetvoice` is re-triggered too: the hardware rewrites the
operators under the running envelope, so the note sounds with the new voice, and a portamento
would keep the old voice's sample.  Green Hill's FM4 and FM5 open the loop body that way (voice
$08 → $05 at the jump label, tick 577); written as `3FF` they rode the intro's last, decayed
note at −50 dB for four rows (first pass) and after every loop-back whatever the song's end
left on the channel — heard as a silent start to pattern 5.  `tools/mod_lint.py` flags a `3xx`
with nothing playable under it, and the regression suite fails a case on any such note its
baseline does not have.

A no-attack note with nothing sounding on the channel yet (no note-on before it) is a real
note-on as well: Drowning's FM3 trill is `smpsNoAttack` from its first note, and written as
`3FF` from row 0 the whole line was silent — `tools/mod_lint.py` reported 240 silent
portamentos there.  After that first note the chain rides one sample, so a chain longer than
the sample (the 10 s auto-sustain cap, `sustain_short`) goes silent where the sample ends
unless `sustain_loops: all` loops it; the lint lists those rows too.

These three rules are `legato: strict` in `settings.yaml`.  `legato: loose`
writes every legato note as a `3FF` on the target's own instrument, as the conversion did
before 2026-09-28 — byte-identical to those builds — which sounds right only in a player that
swaps the sample on an instrument number (OpenMPT); FT2 clone and ProTracker do not, and the
MODs are checked in FT2 clone.  `legato: retrigger` (the default since 2026-09-28) writes every no-attack note as a plain
note-on, as the conversion did before 030ca81 (its pattern cells are those of that build; the
samples keep today's lengths).  What that trades: a grace note bending into a chord is heard
as two attacks, but a legato onto a note that has already sounded for seconds is not left on
the sample's decayed tail.  Green Hill's FM1 shows the second case: E held 56 ticks, a
no-attack rest holds it 56 more, then a no-attack C 2.8 s in — the hardware plays that C at
the level of a fresh note (−13.6 dB in the VGZ), a `3FF` there rides instrument 12's sample
where voice $06 has decayed into a beat null (−39.5 dB).  The sample-side answer (a render
whose sustain decays as slowly as the hardware's, or a loop) is the envelope-tail question
the audits left open; the setting is the note-side one.

### Effect priority (one per note-row)

When multiple effects are active on the same note, **first match wins**:

1. **Cxx — Set Volume** (`smpsAlterVol` result differs from current): volume changes take priority because they affect all subsequent notes until changed again.
2. **4xy — Vibrato** (`smpsModSet/On` active, speed > 0): vibrato is a continuous effect; priority over note-cut.
3. **ECx — Note Cut** (`smpsNoteFill` cut falling inside the attack row): lowest priority.

Implication: if volume changes on the same row as vibrato, vibrato is dropped for that row. Design songs (and YAML configs) to avoid stacking these on the same row.

A cut that lands inside the attack row while that row needs `Cxx` is moved to the start of the next
row (`C00`), or dropped if the next event is already there.  Cuts on later rows of the note have
the effect column to themselves (the `4xy` continuation skips that row).  Exception to the order
above: an attack-row `ECx` does displace `4xy` — a note that short has no audible vibrato.

### Notes that start between rows (`EDx`)

A note whose tick is not a multiple of `ticks_per_row` goes on the row it starts **in**, delayed
with `EDx` (`SmpsToModConverter._note_cell`).  Before, it was rounded to the nearer row — up to half
a row early or late, and because Python rounds halves to even, early and late on alternate notes
(GHZ FM4/FM5 play 115 / 155 notes one tick off the grid).

- **The delay is measured in frames, not average ticks.**  With tempo modifier *m*, `TempoWait`
  holds every *m*-th frame, so tick *k* falls on frame `k + k // (m − 1)` and ticks are unevenly
  spaced.  GHZ (*m* = 3, 2 ticks per row): an odd tick comes 1 frame = 16.7 ms after its row
  starts, not the 25 ms an average tick lasts — exactly `ED1` at speed 3.  Measured: FM4/FM5
  median onset error +17 ms with the average-tick delay (`ED2`), +1 ms with the frame delay.
  `x = round(frames × target_speed × _tpf_at(tick) / _effective_tpr)`.
- **`EDx` needs the cell's one effect slot.**  A cut (note fill, or a PSG note's end) inside the
  attack row keeps it, and the note is rounded as before.  A `Cxx` due on the attack row gives
  way when the note lasts into the next row: the volume is set on the first later row of the note
  with a free slot instead (one row at the instrument's own level — a lost row of level beats
  33 ms of timing; Drowning FM4 pans every other note hard, so half its notes carry a −3 dB `Cxx`
  and all of them start a tick off the grid).  An attack-row `4xy` is given up for it (later rows
  carry the vibrato), and a rest's `C00` on the same row is overwritten — the new note ends the
  old one anyway.
- **Two note-ons cannot share a cell.**  When the row already holds the channel's previous
  note-on — a 1-tick grace note and the note it slides into under `smpsNoAttack` — the later one
  takes the next row, undelayed: late by less than a row (GHZ: 33 ms) instead of erasing the
  grace.  The slide target is still re-triggered; a `3xx` slide is not possible when the two
  notes sit in different `voice_map` ranges, as they do in GHZ.
- DAC notes carry no other effect, so they always get their delay (Title Screen's 2-tick snare
  roll: three hits on three rows instead of two on one).

Songs with off-grid notes: Title Screen (DAC), GHZ (FM3–FM5), Spring Yard (FM4, FM5, PSG1),
Ending (DAC), Chaos Emerald (PSG1, PSG2), Drowning (FM4, FM5); songs whose notes all sit on the grid do not change.
GHZ key-ons with no MOD note row: FM1 4, FM3 2, FM4 15, FM5 15 → 0 on every channel.

### FM levels (`fm_volume_scaling: baked`)

On the chip an FM note's level is set by the track's TL offset — `smpsHeaderFM` volume plus every
`smpsAlterVol` so far, 0.75 dB per step — and by its pan: a centred channel drives both speakers,
a hard-panned one drives one (−3 dB power).  A MOD note's level is its instrument's default
volume unless a `Cxx` overrides it, and instruments cannot share sample data, so a second copy of
a sample at another volume costs its full size.

`SmpsToModConverter._plan_levels(source_map, "FM")` therefore walks the FM channels first — with
the same `DriverState` the conversion uses — and, for every MOD instrument, counts notes per level
`−0.75 × TL − pan`.  The laws themselves live in `core/levels.py`.  The level with the most notes is that
instrument's **baked level**: it is what the `sample_list` volume stands for, and those notes get
no command.  A note at any other level gets `Cxx = volume × 10^(ΔdB / 20)` (clamped to 64).  So:

- `Cxx` appears on the *minority* channel of a shared instrument and where `smpsAlterVol` has
  moved a channel — not on every note, and with the chip's law (a fade stays a fade);
- no variant instruments, no extra sample memory;
- `sample_list` volumes stay hand-set (measure with `tools/vgm_compare.py`); with the law right,
  every channel using an instrument shows the *same* error, which one volume then fixes.
  GHZ: instrument 11 read +4.8 / +4.6 / +4.6 dB on FM3 (centre) / FM4 (left) / FM5 (right);
  16 → 9 brought all three within 0.4 dB.  Whole song: every FM channel within ±0.3 dB.

Across the 18 configs this took `Cxx` on FM notes from 1662 to 887.  Songs that gained commands
had been wrong silently: Drowning's crescendo (`smpsAlterVol` with negative deltas) used to clip
at volume 64 and vanish.

Legacy modes: `fm_volume_scaling: true` (header TL + log law as an absolute volume, `Cxx` on
every FM note) and `false` (header TL ignored, one `smpsAlterVol` step = one *linear* volume unit
≈ 0.3 dB — FM1's GHZ fade drifted 5 dB).

### PSG levels (`psg_volume_scaling: baked`)

Same scheme on the SN76489: level = −2 dB × attenuation (`smpsHeaderPSG` volume +
`smpsPSGAlterVol`, 15 = silent), planned by `_plan_levels(source_map, "PSG")` — the same pass, with the
same `DriverState` instrument tracking the conversion uses (header voice, `smpsPSGform` →
`psg_map`, `smpsPSGvoice` → `psg_voice_map` and its per-note range dispatch).  The attenuation most of an instrument's notes play at needs no command
and is what its `sample_list` volume stands for.  There is no pan term — the PSG is mono.

The legacy mode (`absolute`) made the volume `64 × 10^(−2·att/20) × sample volume / 64`, so every
PSG note whose track attenuation was not 0 carried a `Cxx` — 3241 of 3241 PSG notes across the 18
configs; baked leaves 498.  That is more than tidiness: the effect column is now free on most PSG
notes, so an attack-row note cut no longer has to move to the next row to make room for the
volume (Title Screen 12, Spring Yard 264 cuts restored to the tick).

The configs were migrated when the mode was introduced: each PSG `sample_list` volume became what
its dominant attenuation emitted before (Title Screen noise 16 → 6, i.e. the old `C06`; the change
is recorded in a trailing comment on each line).  Per-note effective volume is identical before
and after on every song except 268 Scrap Brain notes that went 12 → 13 — the single rounding is
the more accurate one (32 × 10^(−8/20) = 12.7).

---

## Tempo commands on row 0 (`_place_tempo_commands`)

The BPM and, when it is not 6, the speed are `Fxx` on pattern 0 row 0.  They are placed
after every channel is converted, into cells whose effect slot is free (spare channels first,
then any channel with no effect on row 0; a leading rest's `C00` gives way if nothing else is
free, with a warning).  They used to be written first, on channels 0 and 1, where a note's own
row-0 effect silently overwrote them: the merged Green Hill build lost its speed 3 to a `Cxx`
and played at half tempo.  Mid-song `smpsSetTempoMod` changes are placed the same way
(`_write_tempo_changes`).

## Loop extension (`_extend_looping_channels`)

A channel whose data ends in a short `smpsJump` loop (typically PSG3's hi-hat) is extended by
replaying the loop body until the song's last tick.  The body is the events **after the jump
label** — `SmpsChannel.label_event_index`, recorded by the parser — not "events at or after the
label's tick": a coordination flag written just before the label shares its tick but is not in
the loop.  Spring Yard PSG3 has `smpsPSGAlterVol $FF` immediately before `Mus85_SYZ_Jump03:`; the
tick-based selection replayed it every repetition and the hi-hat crept from attenuation 5 to 0 in
five loops.  The SYZ VGZ shows attenuation 5 for the whole song.  (Marble Zone: 2 cells.)

---

## Timing: Ticks → Rows → Patterns

### Tick-to-row formula

```
row_total = int(tick_position / ticks_per_row)
pattern   = row_total // 64
row       = row_total % 64
```

`ticks_per_row` is set in YAML (`ticks_per_row: 6` default). Choose it to be the GCD of the note durations in the song so all durations map to whole row counts.

### Common SMPS durations with ticks_per_row = 6

| SMPS duration | Ticks | MOD rows |
|---------------|-------|----------|
| $03 | 3 | 0.5 (avoid) |
| $06 | 6 | 1 |
| $0C | 12 | 2 |
| $12 | 18 | 3 |
| $18 | 24 | 4 |
| $24 | 36 | 6 |
| $30 | 48 | 8 |

### Common SMPS durations with ticks_per_row = 2

| SMPS duration | Ticks | MOD rows |
|---------------|-------|----------|
| $02 | 2 | 1 |
| $04 | 4 | 2 |
| $06 | 6 | 3 |
| $08 | 8 | 4 |
| $0C | 12 | 6 |
| $10 | 16 | 8 |

### BPM and speed setup

MOD timing is set by two `Fxx` effects placed in Pattern 0, Row 0:
- **Channel 0:** `Fxx` where xx = BPM (range $20–$FF = 32–255 BPM)
- **Channel 1:** `Fxx` where xx = speed (range $01–$1F = ticks per row)

These are written automatically by `smps2mod.py`. See `docs/yaml_config.md` §BPM Derivation for the formula and `auto_bpm` option.

**Whole-number BPM.**  The formula rarely lands on an integer, and the MOD's BPM is one:
Special Stage (modifier 8, divider 2, 2 ticks per row) at speed 3 is 98.4375 → 98, which ran
0.44 % slow — 139 ms behind the hardware over its 33 s pass; Star Light and Chaos Emerald were
187.5 → 188 (+0.27 %).  `target_speed` only changes how many MOD ticks a row has, never the row
grid, so it is free to choose: speed 4 makes those two exactly 250, speed 6 makes Special Stage
196.875 → 197 (+0.06 %).  `convert.py` prints the exact value, the error in ms per minute and
the speed that would do better (`core.config.bpm_rounding_options`); the `analyze.py` skeleton
picks that speed.  Residuals now: Invincibility, Continue and Game Over ±0.07 %, everything else
exact.

**Mid-song tempo changes** (`smpsSetTempoMod`, $EA — Drowning ×4, Credits ×5).  The flag sets
every track's modifier and restarts the TempoWait counter.  The converter collects the changes
(`_collect_tempo_segments`), scales the BPM by the change in tick rate and writes `Fxx` on the
row of each change — in a spare MOD channel when there is one, else any cell without an effect,
else a cell holding only a `4xy` continuation (`_write_tempo_changes`; a song that loops back into
another segment gets an `Fxx` at the loop target too).  Note fills, vibrato rates and `EDx`
delays use the modifier in force at their tick (`_tpf_at`).  Every segment's BPM must fit
32–255: Drowning goes 75 → 100 → 112 → 125 → 135 at 2 ticks per row (1 tick per row would need
270 at the end); `convert.py` warns when a segment is clamped.

**Global duration divider** (`smpsSetTempoDiv`, $EB — Credits' half-tempo passage, written from
the DAC track).  `cfSetTempoDividerAll` writes every track's `TempoDivider`; the driver multiplies
a duration by it when the note is *read*, so a note begun before the change keeps its length and
the last write wins against the track's own `smpsChanTempoDiv`.  `_apply_global_tempo_div`
re-times every channel accordingly before anything reads ticks (the carrying channel first, since
the change's real tick depends on any earlier change; the parser keeps `smpsChanTempoDiv` as an
event so the divider each note was parsed with is known).  Rows stay ticks: the passage simply
has twice as many rows, at the same BPM.  Labels (loop targets) are not re-timed — no song that
uses the flag loops.

*Inherent:* the driver's holds come at the end of each counter cycle, so the first frames after a
change run a little fast and the MOD ends up one to two frames (17–48 ms) behind at each change,
flat in between — Drowning is +59 ms behind by its end.  The audit tools follow that; a listener
has nothing to compare it with.

---

## MOD Binary Format Quick Reference

```
Offset   Size   Content
0        20     Song name (ASCII, null-terminated, max 19 chars)
20       930    31 sample headers (30 bytes each)
950      1      Number of patterns used (1–127)
951      1      Always 127 (ProTracker sentinel)
952      128    Position list (pattern indices, 0-based)
1080     4      Format tag: "M.K." (4ch), "8CHN", "10CH", "16CH"
1084     …      Pattern data (N patterns × channels×4×64 bytes each)
…        …      Sample PCM data (concatenated, 8-bit signed)
```

### 4-byte note cell

```
Byte 0:  [inst high nibble][period high byte high nibble]  = (inst>>4)<<4 | period>>8
Byte 1:  period low byte
Byte 2:  [inst low nibble][effect command nibble]
Byte 3:  effect parameter
```

Period values come from `PERIOD_TABLE` in `tables.py` (ProTracker PAL periods, C1–B3).
Instrument is 1-based (1–31); 0 = no instrument (continue previous).

### MOD limits

| Resource | Limit |
|----------|-------|
| Samples/instruments | 31 |
| Patterns | 127 |
| Rows per pattern | 64 |
| Channels | 4, 8, 10, 12, 14, or 16 (format tag required) |
| Sample size | 131070 bytes (65535 words × 2) in the format; original ProTracker's editor takes 65534. `max_sample_kb` (settings.yaml, 128 or 64) is what the generators cap each instrument's sustain to, and `sample_truncated` warns if one is cut anyway |
| Note range | C1–B3 (36 semitones) |

---

### `range_space: chip` — ranges on the pitch the chip plays

`voice_map` / `psg_voice_map` ranges are matched against the source byte (`note − $81`) by default,
and `root` anchors that byte.  A song that changes key with `smpsChangeTransposition` while
keeping a voice breaks that model: the same byte must reach different MOD notes.  Credits' FM2
does it twenty times, and the whole medley moves voices between octaves; matched on source bytes
it audited at 8 % of notes right.  With `range_space: chip` (song-level) the key is the real pitch
— byte + pitch_offset + accumulated `$E9`, PSG through the driver's frequency table
(`core.driver_tables.psg_index_semitone`) — so `low`/`high` are chip pitches, `synth_root` is simply `low`,
and a voice spanning more than three octaves gets one entry per window.  Two voices sharing one
sample keep separate entries: `root_e = root_head + (low_e − low_head)`.  `configs/13_credits.yaml`
is generated this way (1623 of 1635 notes right; the 12 left are detune scoops the converter does
not do, and PSG notes transposed below the table).

`tools/config_to_chip_space.py <config>` converts an existing source-space config: for every
entry it collects the chip pitches each channel plays through it (loops extended, every
transposition), makes one entry per touching range with `synth_root` = the range's low note and
`root` shifted so the tuning `synth_root − root` is unchanged, and warns where a range cannot
fit MOD C1–B3 at that tuning — those notes need an instrument of their own (Ending's PSG2 plays
E6–B6 on a sample that reaches B3+24 at most).  Stage Clear, Ending, Invincibility and Continue
were converted this way (62 → 74, 190 → 194, 140 → 204, 70 → 101 notes right); Star Light by hand.
Source space is still what the other configs use.

## voice_map Routing

### Decision tree

```
Note event: source_semitone = (note_byte − $81); current_voice = active voice index
                │
                ▼
        voice_map[current_voice] exists?
        ├── YES → scan InstrumentRange list for matching [low, high]
        │         ├── MATCH FOUND:
        │         │     mod_instrument = entry.mod_instrument
        │         │     entry.root set?
        │         │     ├── YES: output = root + (source_semitone − entry.low)
        │         │     │         [clamped C1–B3; ignores total_transpose]
        │         │     └── NO:  output = source_semitone + total_transpose
        │         │               [total_transpose = header pitch + smpsChangeTransposition]
        │         └── NO MATCH: fall through ↓
        └── NO  → channel_instrument_map[source][current_voice] exists?
                  ├── YES → same range scan logic
                  └── NO  → use channel default instrument + total_transpose
```

### When to use `root`

Use `root` when:
- The channel does **not** use `smpsChangeTransposition` mid-song, OR
- You want a fixed anchor regardless of transposition (e.g. FM2 bass in GHZ).

**Do NOT use `root`** when:
- The channel uses `smpsChangeTransposition` ($E9) dynamically — the notes will land at `root + offset` regardless of the transposition, which may be incorrect.
- Example: GHZ FM1/FM3/FM4 use `smpsAlterPitch` to shift register ranges mid-song. These channels must use the `total_transpose` path (no `root`).

### root formula

```
output_note = entry.root.value + (source_semitone − entry.low) − entry.synth_shift
```

- `root` is **unconditional** — `smpsDetune`, header pitch_offset, and `smpsChangeTransposition` do NOT affect the root path.
- `synth_shift` is 0 unless the entry states a `synth_root` other than the pitch the chip plays for `low` (`resolve_synth_roots`); then `root` is the note where `synth_root` sounds and the notes move down by the difference (`docs/fm_synthesis.md` §Pitch).
- Ensure `root + (high − low) − synth_shift` stays within C1–B3 (values 0–35) to avoid clamping.

### Choosing root placement

Higher `root` value → higher `target_rate` → better synthesis quality (more audio frequency resolution at Amiga sample rate).

```
target_rate = round(amiga_clock / PERIOD_TABLE[root.value])
```

Example — source C5–B6 (span = 12 semitones):
- `root: C2` → output C2–B2, target_rate ≈ 8,287 Hz (acceptable)
- `root: C3` → output C3–B3, target_rate ≈ 16,574 Hz (better quality)
- Choose the highest `root` that keeps the full range within C1–B3.

---

## Common Gotchas

### 1. smpsAlterNote / smpsDetune is NOT semitones

**Problem:** voice_map range doesn't match expected notes after `smpsAlterNote $03`.

**Cause:** `$E1` adds a raw FNUM offset (~10 cents per unit). It is NOT a semitone shift and does NOT affect `source_semitone` used in range lookup.

**Fix:** Ignore `smpsAlterNote` when writing `voice_map` ranges. For detuned-unison chorus channels (e.g. FM5 vs FM4), route to a `mod_instrument` with `finetune: 1` (≈ +12.5 cents) via `channel_instrument_map`.

---

### 2. smpsChangeTransposition mid-song breaks root anchoring

**Problem:** Notes land at unexpected pitches after a `smpsChangeTransposition` event.

**Cause:** `root` is unconditional — it anchors `source_low` to a fixed MOD note regardless of `total_transpose`. If `smpsChangeTransposition` changes the chip pitch mid-song, the root path gives wrong output.

**Fix:** For channels that use `$E9` dynamically, omit `root` from `voice_map` entries and rely on `total_transpose`. If you need synthesis accuracy, set `synth_root` to the actual chip pitch.

---

### 3. smpsNoteFill longer than a row is a `C00` / `ECx` on a later row

**Problem:** Expecting every `smpsNoteFill` to show up as `ECx` next to its note.

**Cause:** `ECx` can only cut within its own row (x < speed).  The converter works out the cut
position in absolute MOD ticks and writes it on whichever row it falls in: `ECx` when it is inside
a row, `C00` when it is exactly on a row boundary.  There is no upper limit on the fill value
(GHZ FM3/FM4 use `$1E` = 500 ms).

**When no cut is written:** the fill outlasts the note (`fill × (mod−1)/mod ≥ duration`, the
driver's DurationTimeout expires first), or the cut would land on the next event's row.

---

### 4. smpsModSet → `4xy`: rate from the cycle in frames, depth per note

**The driver** (`DoModulation`, once per V-int frame, after `wait` frames): every `speed` frames
it adds `delta` to an accumulator; when the step counter reaches 0 it reloads it from the
**original** `steps` byte, negates `delta` and spends that update.  Only the first half-swing uses
the halved count (`lsr.b #1` on `smpsModSet` / note start).  So:

- steady cycle = `2 · speed · (steps + 1)` **frames** — not ticks, and not multiplied by the tempo
  divider (the parser used to multiply `speed` by it);
- swing = `delta · steps / 2` either side of centre, in the units of the note's own frequency
  word, which it is added to: the YM2612 FNUM of the note's pitch class (644 for C … 1216 for B)
  or, on a PSG channel, the SN76489 divider from `PSGFrequencies`.  The same `smpsModSet` is
  therefore deeper in cents on C than on B, and enormous on a high PSG note (Stage Clear's last
  PSG1 note: divider 127 ± 16 = ±200 c at 6 Hz — real, it is in the register log).

**ProTracker:** the vibrato position advances by `x` on each of a row's `speed − 1` processing
ticks and wraps at 64; the sine peaks at about `2·y` period units.

**Conversion** (`SmpsToModConverter._vibrato_speed` / `_vibrato_depth`):

```
x = 64 · _effective_tpr / ((target_speed − 1) · cycle_frames · _tpf_at(tick))
y = period · (delta · steps / 2) / frequency_word / 2          (per note)
```

Region-independent.  `y` below 0.35 means the smallest depth would overshoot the hardware
threefold, so no vibrato is written (Spring Yard FM4/FM5: ±3 c on hardware).  When `x` would
exceed 15 `convert.py` says so.  A per-entry `vibrato:` override still wins, but none is needed
any more: the eight that existed were workarounds for the old formula and are gone.

**Measured** (hardware → MOD): Title Screen FM4 5.99 Hz ±19 c → 5.99 Hz ±18 c (`4C3`; was `485` =
3.98 Hz ±36 c); GHZ PSG1 7.35 Hz ±7 c → 7.44 Hz ±7 c (was 4.98 Hz); Scrap Brain FM1 4.96 Hz ±54 c →
5.23 Hz ±53 c; Spring Yard FM1 5.99 Hz ±25 c → 6.24 Hz ±19 c; Stage Clear FM5 4.99 Hz ±15 c →
4.99 Hz ±14 c; Special Stage 4.25 Hz ±20…32 c → 4.06 Hz ±20…30 c.  What is left is the 4-bit
grid: one step of `x` is 0.4–0.6 Hz, one step of `y` is 10–30 c depending on the period.

**Known inaccuracy (measured 2026-09, see `docs/audits/01_title_screen_audit.md` §2):** the current
speed/depth formula runs the LFO too slow and too deep — Title Screen FM4 `smpsModSet $00,$01,$06,$04`
is 5.75 Hz / ±19 cents on hardware but `485` = 3.85 Hz / ±33 cents in the MOD; `4C3` would be right.
Modulation timers count V-int **frames** (60 Hz), not tempo ticks; the steady cycle is
`2·speed·(steps+1)` frames with amplitude `delta·steps/2` FNUM units, and ProTracker's cycle is
`64/x` processing ticks (`speed−1` per row) with amplitude ≈ `2·y` period units.

---

### 4b. smpsNoteFill counts frames, not ticks

**Problem:** Note cuts land late on songs with a tempo modifier (Title Screen fill `$0C` cuts at
250 ms in the MOD, 200 ms on hardware).

**Cause:** `TempoWait` only delays `DurationTimeout`; `NoteTimeoutUpdate` still runs every V-int,
so the fill value is in frames (60 Hz) while durations are in ticks (`fps × (mod−1)/mod`).

**Fix (done):** `SmpsToModConverter._tpf(modifier)` = `(mod−1)/mod` (1.0 for SFX / mod ≤ 1); `_tpf_at(tick)` picks the modifier in force at a tick, so mid-song `smpsSetTempoMod` is honoured
converts frame counts to ticks; the fill and the `smpsModSet` wait both go through it
(`×0.8` for tempo modifier 5, `×0.667` for GHZ's 3).  The same ratio decides whether the fill fires
at all: a fill equal to the duration byte **does** fire when the song has a tempo modifier, because
the note lasts `duration × mod/(mod−1)` frames.

Verified against hardware key-off timing in the GHZ VGZ (YM2612 reg `$28` writes): FM2
`smpsNoteFill $04` → 158 key-offs at exactly 4 frames (67 ms; the old output cut at 100 ms), FM1
`$0B`/`$14` → 11/20 frames, FM3/FM4 `$1E` → 30 frames (previously no cut at all, since 30 ≥ the
24-tick duration), PSG `$06`/`$10` → 100/267 ms (were 150/400).  Title Screen noise cuts: worst
error vs the recording +75 ms → +15 ms.

**Related parser fix:** `_scale_effect_params` used to multiply the `smpsModSet` wait by the tempo
divider.  The driver never does (`ModulationWait` is a raw frame count), so divider-2 songs had
the vibrato onset twice as late before the tick error was even added — Special Stage `$1A` started
at ~990 ms instead of 433 ms.

**Vibrato rows:** a row carries `4xy` when modulation is running for at least half of it, the
attack row included — so notes shorter than the wait no longer get vibrato at all (they used to
get it on the attack row unconditionally).

Mid-song `smpsSetTempoMod` is followed (§BPM and speed setup); the rate and depth of the `4xy`
itself are gotcha 4.

---

### 5. Wrong operator order → distorted FM synthesis

**Problem:** Synthesized FM samples sound like an overdriven guitar / extreme distortion.

**Cause:** Wrong `SMPS_OP_TO_REG_OFFSET` mapping (`core/driver_tables.py`). SMPS stores operators in reversed order (OP4,OP3,OP2,OP1); the correct mapping is `(0x0C, 0x04, 0x08, 0x00)`. The wrong mapping `(0x00, 0x08, 0x04, 0x0C)` puts OP1 (often TL≈$01, near max volume) into the self-feedback slot.

**Fix:** Verify `SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)` in `core/driver_tables.py` — `ym2612/voice.py` and `sfx/chips.py` both read it from there. Do not change it.

---

### 6. Synthesized FM samples are silent by default

**Problem:** FM channels produce no sound even after configuring `voice_map`.

**Cause:** `synthesis.enabled: false` in `configs/settings.yaml` (default). Synthesis requires compiling `ym3438.c` via gcc or MSVC.

**Fix:** Set `synthesis.enabled: true` in `configs/settings.yaml`. Or load pre-rendered `.raw` files via `sample_list` without enabling synthesis.

---

### 7. Loop Bxx must be written after apply_pattern_breaks

**Problem:** Loop point lands in the wrong pattern, or a D-row companion effect is needed unnecessarily.

**Cause:** `_set_loop_point()` uses post-break MOD coordinates. If called inside `convert()` (before `apply_pattern_breaks`), the Bxx is placed at a pre-break row number that gets displaced during repacking. The target tick-to-pattern conversion also ignores the row offset that the break introduces.

**Fix:** Call order must be:
```
converter.convert()                                  # writes all note data; does NOT call _set_loop_point
apply_pattern_breaks(mod, config.mod_pattern_breaks) # repacks stream
converter._set_loop_point(config.mod_pattern_breaks) # writes Bxx in final layout
```
`_set_loop_point(breaks)` accepts the breaks list so it can apply the coordinate-remapping formula (see §Pattern Breaks).

---

### 8. FM5 fall-through into FM1 data

**Problem:** FM5 appears to contain FM1's note data.

**Cause:** In many Sonic 1 songs, FM5 contains only a `smpsAlterNote` or `smpsAlterPitch` command then implicitly falls through to FM1's label (no `smpsStop`). The parser does not stop at label boundaries — only `smpsStop` or `smpsJump` terminate a channel.

**Fix:** This is correct behavior, not a parser bug. FM5 deliberately shares FM1's data with a pitch offset (chorus/detune effect). Configure FM5 with the same `voice_map` as FM1, possibly adding a `finetune+1` instrument variant for the FNUM detune.

---

### 9. Effect priority conflict silences vibrato

**Problem:** Vibrato (`4xy`) disappears from rows where `smpsAlterVol` fires.

**Cause:** Volume changes take priority over vibrato in `smps2mod.py`. If both are active on the same row, `Cxx` is emitted instead of `4xy`.

**Fix:** This is a fundamental MOD limitation (one effect per note). In practice, `smpsAlterVol` events are sparse; vibrato is continuous. The loss is usually inaudible. If critical, split the channel to a second MOD channel (use `channel_instrument_map` for the volume-change section).

---

### 10. Standalone dc.b duration byte ≠ rest

**Problem:** A bare `dc.b $0C` line after an `smpsNoteFill` advances time but no note appears.

**Cause:** A duration byte with no preceding note on the same `dc.b` line emits a continuation event. Without a preceding `smpsNoAttack`, the parser **retriggles the last note** (`is_rest=False, note_value=last_note_value`) — e.g. staccato arpeggio in 1-Up. With a preceding `smpsNoAttack`, it emits a rest/sustain (`is_rest=True, is_no_attack=True`) — e.g. held note in GHZ. This is correct per the SMPS driver behavior (`FMNoteOn` is gated by the no-attack flag).

**Fix:** No fix needed — this is working as designed. The implicit wait correctly represents the held note duration.

---

### 11. A leading rest needs its `C00` at pattern 0 row 0

**Problem:** Robotnik and Special Stage loop to position 0.  On every pass after the first, the
last note before the `Bxx` kept ringing through the channel's opening rest (6.4 s on Robotnik's
FM channels) until the channel's next event.

**Cause:** The emitter skipped the `C00` of a rest at pattern 0 row 0 because nothing plays there
on the first pass and that row holds the `Fxx` speed / BPM commands.

**Fix:** `_place_leading_rests` runs after all channels are converted: it writes the `C00`, and
moves an `Fxx` in the way to a free cell on row 0 (spare channels first, then any cell without an
effect).  Only when no cell is free is the `C00` dropped, and only if the song does loop back to
row 0 (`_loop_target_tick() == 0`) is that a `rest_no_slot` warning — Star Light rests on all
nine channels but loops to position 1.

---

### 12. A note outlasts its sample (`sustain_duration: auto`)

**Problem:** Synthesised samples do not loop, so a note longer than the sample goes silent.

**Cause / rules:** `_sustain_needs` measures the longest ring per instrument in the MOD's own
time (tempo segments, after `smpsSetTempoDiv` re-timing), at the sample's playback rate (root
period / note period against the **first** entry's root, the one the sample is rendered for),
with a positive finetune and one row of margin.  The auto sustain is the largest need, capped
at 10 s; each generator also caps every instrument to the sample limit at its rate
(`max_sample_kb` in settings.yaml: 128 = the format's 131070 bytes, 64 = original
ProTracker's 65534).  `sustain_short` warnings name what is left.  Full rules: `docs/fm_synthesis.md`
§ `sustain_duration: auto`.  With sustain loops on (below) a looped instrument holds any
note and warns nothing.

---

## Sustain loops and release slides (`sustain_loops`, `core/loops.py`)

`sustain_loops` in `settings.yaml` (`off` | `merged` — the default: the `--merged` build only |
`all`) makes a sample's length independent of the notes it plays, the one structural thing a
hand-made Amiga MOD does that a plain render cannot.

**The loop.** After rendering, `core.loops.find_sustain_loop` looks at the RMS envelope of the
sustain (windows of two fundamental periods, dB below the sample's peak).  The reference is the
`SPAN_SECS` (1 s) before the end of the instrument's longest note (`ref_n`; the span after that
note's end when the note is shorter than the span, never the attack); the envelope is *flat*
from the first window after which every window stays within `loop_drift_db` of the level at the
span's end, widened by the span's swing around its trend (detrended: a decaying voice's span
must not pass its whole decay, and its attack, as flat).  A band, not a level, so a chorus pair
that beats is flat once the beating is steady.
Loop candidates start at the flat point and run every even length (a MOD loop is measured in
words) from 30 ms to `MAX_LOOP_SECS` (1.2 s: a detuned pair beating at 1 Hz needs a whole
beat), scored by the discontinuity the loop would introduce — the RMS difference between the
two periods after the loop start and the two after its end — plus `LENGTH_PENALTY` per second.
A whole number of fundamental cycles is rarely the answer: nearly every Sonic 1 voice detunes its
operators (DT1), so the waveform never repeats exactly (voice $04's best raw match is a −20 dB
jump); the best length is where the operators' phases come closest to recurring.  `apply_loop`
then crossfades the last 15 ms of the loop into the samples before its start and cuts the
sample at the loop's end.  numpy scores every length at once; without it only the fundamental's
grid is tried.

Generators render a `PROBE_SECS` (4 s) sustain to search in and fall back to the note's own
length without a loop.  No loop is made where the level at the reference has decayed below
−50 dB (a percussive voice), where the loop would end later than the plain render (a voice
still settling: `MAX_END_FRACTION`, and the sustain-plus-release length), or where the raw
discontinuity is over `MAX_ERROR` (−2.5 dB, i.e. uncorrelated).  `loop_drift_db` (1 dB default)
is the fidelity knob: a slowly decaying voice (Green Hill's $00, $06, $08: carriers with a
sustain rate of 3–7) loops only where its last second is within that of the loop point, so at 1 dB
its long notes keep their decay and its sample stays long; at 3–6 dB it loops earlier and its
longest notes end that much louder than the hardware's.  Green Hill merged without its chime
mixes: 1 dB → 184 KB of samples, 6 dB → 178 KB, 12 dB → 140 KB (unlooped 253 KB; the size is
not monotonic in the drift, because an earlier flat point changes which loop scores best).  PSG tones are looped
the same way (`sn76489/sample_generator.py`); noise never is.

**The release.** A looped sample rings until something stops it, and a plain sample is cut
where the hardware released, so with loops on every FM note ends with a volume slide instead
of `C00`: `release_rate_db_s` fits the dB-per-second slope of the render's tail after key-off
(the `release_padding`), and `SmpsToModConverter._write_release` writes one `A0y` per row from
the rest's row (or the note fill's row, whose sub-row `ECx` position is given up) until the
volume is gone or the next note-on's row.  The chip's release is linear in dB, so each row's
target is the last row's times a fixed ratio and `y` is what takes the volume there
(`(speed − 1)` slide ticks per row; rows whose share rounds to 0 are skipped so a slow release
keeps its pace); after 64 rows a `C00` ends what is left (release rate 0 rings forever on the
hardware).  A release that is over within a row (`RR $0F`: every Title Screen voice) stays a
`C00`/`ECx`, and PSG notes keep their cuts (the driver sets attenuation 15 at once).  A mixed
composite ends the way its primary does.  The vibrato continuation stops at the slide.

Two things the slides made visible: a rest whose row rounds onto the next note-on's cell used
to leave its `C00` there (`set_note` keeps the effect bytes: a silent note), now cleared by
`_clear_stale_cut`; and the slides never reach a note-on's row (they stop at the row before the
next note-on tick), so the two cannot collide.

---

## Pattern Breaks (`mod_pattern_breaks`)

### Purpose

A SMPS song often has a short intro (e.g. 32 rows) followed by a long loop body. Without breaks,
the intro and body share Pattern 0, leaving 32 blank rows of silence at the end of the pattern on
every loop iteration.

`mod_pattern_breaks` inserts a `Bxx` jump after the intro rows and repacks the body data into
fully-packed patterns, eliminating the wasted rows.

### YAML config

```yaml
mod_pattern_breaks:
  - pattern: 0    # which pattern to split
    row: 31       # last intro row; Bxx written here, body starts at row+1
```

Parsed as `[(0, 31)]` — a list of `(pattern_slot, row)` tuples.

### What apply_pattern_breaks does

1. Extracts the **body**: all rows after `(P, break_row)` across all patterns P..N.
2. Repacks body into patterns P+1, P+2, … (fully 64 rows each; extra pattern appended if needed).
3. Clears rows `break_row+1..63` of pattern P.
4. Writes `Bxx → P+1` at `(P, break_row)` on the first free channel.
5. Updates any pre-existing `Bxx` effects that targeted old post-break patterns (remaps coordinates).

**Critical:** Do NOT write any `Bxx` loop-jump before calling `apply_pattern_breaks`. The remapping
in step 5 only works correctly if the loop Bxx does not exist yet — write it afterward via
`_set_loop_point(breaks)`.

### Coordinate remapping formula (single break at (P, break_row))

```
body_start = P * 64 + break_row + 1   # first flat row of the body stream

# pre-break flat row T → post-break position:
if T < body_start:
    pat, row = T // 64, T % 64        # still in intro portion (unchanged)
else:
    br = T - body_start
    pat, row = P + 1 + br // 64, br % 64
```

Example — GHZ, break at (0, 31) → body_start = 32:
- Loop target at pre-break flat row 288: br = 256 → pat=5, row=0 → **B05** ✓
- Loop target at pre-break flat row 287: br = 255 → pat=4, row=63 → **B04 + D63** (Dxx companion needed)

### _set_loop_point(breaks) algorithm

1. **Bxx location** — scan `self.mod.patterns` backward for the last row where any channel cell
   has a non-zero period (`((byte0 & 0x0F) << 8) | byte1 != 0`). This is the last row with actual
   note data in the post-break layout. Bxx is placed there on channel 0.

2. **Bxx target** — apply the coordinate formula above to `loop_target_tick`:
   ```
   flat_row = int(round(loop_target_tick / ticks_per_row))
   # then apply formula → (target_pattern, target_row)
   ```

3. **Dxx companion** — if `target_row != 0`, write `Dxx` (BCD-encoded row) on the next free
   channel at the same row so playback resumes at the correct row within the target pattern.

---

## Verifying against a VGZ (`tools/vgm_compare.py`)

### VGM comparison setup

`reference/vgz/` is **untracked** (`.gitignore`): it holds the reference recordings
(`01 - Title Theme.vgz`, …) and the VGMPlay binaries, neither of which belongs in the repo.
On a fresh checkout:

1. Put the VGZ rips of the songs you want to audit in `reference/vgz/`.
2. Unzip a **VGMPlay 0.51.x** Windows build (Valley Bell's libvgm-based player, source at
   <https://github.com/ValleyBell/vgmplay-libvgm>) into `reference/vgz/vgmplay/` so that it contains
   `VGMPlay64.exe` (or `VGMPlay.exe`), `VGMPlay.ini` and `zlib1.dll`.  The 0.51 line is required:
   the tool patches `VGMPlay.ini` with `Core = NUKE` / `MuteMask = …`, and the older 0.40.x
   "legacy" builds use a different ini layout.
3. Install an ffmpeg build that includes the libopenmpt demuxer (the gyan.dev *full* build does;
   check with `ffmpeg -h demuxer=libopenmpt`) and `pip install numpy`.

VGMPlay is looked up as `--vgmplay DIR` → `VGMPLAY_DIR` environment variable →
`reference/vgz/vgmplay/`, so with the layout above no flag is needed.

### Running it

```bash
python convert.py configs/01_title_screen.yaml
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz"
python tools/vgm_compare.py <cfg> <vgz> --skip-render        # reuse output/compare/<cfg>/*.wav
```

Sections of the report: per-note pitch/level, per-channel summary, **pitch verdict**, **vibrato**,
channel balance, **per-instrument levels**, onset timing, noise (decay + band profile), DAC (rate check).

**Pitch verdict** is `tools/vgm_pitch_audit.py` run inside the report: the chip's frequency-register
timeline against the pitch each MOD note sounds at, with the per-instrument verdict ("synth_root is
1 octave too high" vs "mixed").  It is the authority on "is every note right", and what
`--fail-pitch-cents` tests.  The per-note `vgm_c` / `mod_c` columns are audio measurements and only
a cross-check: both are taken on the strongest of the note's first four partials (an FM voice whose
carriers use a frequency multiple of 2 or more has nothing at the register frequency — measuring
there read −50…−110 c of pure leakage on GHZ FM1), and `<-- PITCH` flags the two *renders*
disagreeing by more than 25 c.  What is left after that is grace notes: the window holds the next
pitch on hardware and a row-quantised one in the MOD (todo item 3).  GHZ: 453 flags → 25.
Notes the recording plays once the MOD's single pass has ended (its second time round the loop) are
left out of every table.

**Per-instrument levels** is the table to set `sample_list` volumes from.  Every note is matched
to the MOD instrument (and `Cxx`) that plays it, and the level error MOD − VGM is reported per
instrument with a per-channel breakdown:

```
inst sample            vol notes    err spread suggest   per channel (Cxx: err xnotes)
  11 ghz_v05_lo.raw     16   116   +4.6    0.2       9   FM3 C1D: +4.8 x20  FM4: +4.6 x58  FM5: +4.6 x58
```

- Only notes **without** a `Cxx` say what the instrument's own volume should be;
  `suggest = volume × 10^(−err/20)`.
- In the baked volume modes every channel sharing an instrument must show the same error.
  `spread` is the disagreement; above 3 dB no volume is suggested — it is not a volume problem
  (look at pan, note fills, a wrong instrument, the alignment).
- Errors are relative to the song's median note, so only *relative* imbalance shows.  The DAC
  (fixed samples at volume 64, cannot be turned up) becomes the anchor only when it is 2 dB or
  more off that median; a smaller gap is within what short DAC hits can be measured to.
- Levels are L/R power, never a mono mix: a hard-panned YM2612 channel reads ~5 dB low in a
  mono mix while every MOD channel loses the same 1 dB.

`--write-volumes` rewrites the config's `sample_list` volumes to the suggestions (errors of 1 dB
or more) and records `# VGZ: +4.6 dB at 16` on the line.  Re-convert and re-run to verify.

`tools/measure_volumes.py` does that for every song at once (cores − 1 in parallel, one process
per song): convert, one `--write-volumes` pass, re-convert, verify, then one line per song with
the volumes changed, what is still 1 dB or more off (instruments at the 64 ceiling, channels that
disagree and two-note instruments are marked as such) and the pitch verdict.  Run it after any
change to how samples are rendered.  It makes exactly one write pass: the errors are relative to
the song's median note, so once many instruments move the frame moves with them and a further
pass drifts the whole song.  The reference renders are reused whenever they exist.

**Vibrato table.**  Every FM / PSG-tone note of 0.5 s or longer is pitch-tracked in both renders
(one partial isolated by heterodyne + brick-wall filter, instantaneous frequency from the phase
derivative).  A row is printed when either side modulates: rate in Hz and depth as ± cents, measured
only over the modulated stretch so `smpsModSet` wait times do not dilute it.  Flags: `VIBRATO`
(rate off by > 15 % or depth by > 5 c / 30 %), `MISSING in MOD`, `not in VGM` (MOD-only wobble —
a `4xy` the hardware does not have, or a sample loop that is not a whole number of periods).
Rows marked `b` are **beating**, not vibrato: two detuned FM carriers wobble a partial's phase
periodically too, but they also swing its level at the same rate (≥ 15 % → `b`; real vibrato
measures ~3 %).  A beat's rate follows sample playback speed, so a `BEAT RATE` flag points at
`synth_root` / multi-sampling, never at `4xy` — GHZ FM4/FM5 C6 (4.46 Hz on hardware, 6.5 Hz in the
MOD) have no `smpsModSet` at all.  PSG notes are covered: the SN76489 has no key-on, so
`vgm_analyze._parse_vgm` starts a PSG note when the channel becomes audible or its period moves more
than 70 cents from where the note started — smaller moves are the driver's modulation and stay
inside the note (GHZ PSG1 `smpsModSet $0E,$01,$01,$03`: 7.35 Hz ±7 c on hardware, theory 7.5 Hz;
MOD 4.98 Hz).
Reference points: the driver's steady cycle is `2·speed·(steps+1)` frames, ProTracker's is
`x·(speed−1)·BPM / (160·speed)` Hz.  Title Screen FM4 closing A2: hardware 5.99 Hz ±19 c
(theory 6.0 Hz); MOD `485` 3.98 Hz ±36 c before the formula was fixed, `4C3` 5.99 Hz ±18 c after.

**CI use.**  `--json FILE` writes everything in the report (per-note rows, channel summaries,
vibrato, noise bands, DAC peaks) plus a `checks` list and an overall `passed`.  Thresholds are
opt-in, and any failed one makes the exit code 1:

| Flag | Fails when |
|------|-----------|
| `--fail-balance-db DB` | a channel's level relative to `--ref` differs from the recording by more than DB |
| `--fail-pitch-cents C` | the pitch verdict has a wrong note (more than C cents from the chip register) or a missing one, or a note is silent in the MOD render |
| `--fail-unmatched N` | a channel has more than N chip key-ons with no MOD note row within 40 ms (DAC: detected audio onsets) |

```bash
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz" \
       --json output/compare/title.json --fail-balance-db 2 --fail-pitch-cents 25
```

**Onset timing** matches each FM / PSG / noise key-on in the register log to a MOD note row, one
to one, so `unmatched` is a count of notes the MOD really lacks or places more than 40 ms off, and
`MOD-only` counts rows the chip has no key-on for (a re-trigger where the hardware ties); the times of
unmatched key-ons are listed under the channel (JSON `unmatched_s`).  MOD rows carrying `EDx` are
timed at their delayed start.  A chip key-on only counts as a note when the channel was keyed off
or the pitch moved by more than 70 cents: the driver's `FMNoteOn` writes key-on unconditionally,
and under `smpsNoAttack` only the key-OFF is skipped, so ties (GHZ `nA5, $10, smpsNoAttack, $3B`)
and tied `smpsDetune` scoops (Scrap Brain FM4, +36 c) log a key-on the chip ignores.  Title
Screen: 0 unmatched on every FM channel; GHZ read FM1 4, FM3 2, FM4 15, FM5 15 before todo item 3
(grace notes, off-grid notes and the tie key-ons above) and reads 0 since.  The 40 ms window follows the running deviation of the notes matched so far, and the
change from the song's first notes to its last is printed as `drift` when it reaches 20 ms: a
MOD that runs slightly off the driver's tempo is one finding, not a lost note per bar (Special
Stage read +139 ms over its 33 s pass before its speed was changed, 0 unmatched).  The matcher
also re-syncs across a step of up to 120 ms when the next two notes confirm it (a tempo change).
An audio onset detector cannot do this on sustained channels (it read 28–143
unmatched per GHZ channel with every note in place).  The DAC still uses it — the log holds PCM
seeks, not hits (GHZ has two seeks 20 ms apart and hits with none) — so its count stays
approximate (Title Screen 3, GHZ 60).

## Channel merging (`merge:`, `convert.py --merged`)

The Amiga build folds SMPS channels onto one MOD channel (`core/merge.py`; config in
`docs/yaml_config.md` § merge). A group names a **primary** and its **followers**; the followers
leave the output and the primary plays a composite instrument wherever a follower sounds with it.

**Per primary note-on at tick t**, with the follower's note-ons as `walk_channel` resolves them
(`smpsNoAttack` continuations extend a note; a rest ends it; a drum or noise note *sounds* for
its sample's length when that is shorter, `NoteOn.sounding`, from the drum file's size at its
`mod_note` and the noise envelope's frames). A follower note-on within `merge_tolerance` ticks
of the primary's counts as at t (`match_onsets`, nearest first, each follower note once), and a
note of at most that many ticks followed by an `smpsNoAttack` note is a grace note bending into
it: the two are one note at the target pitch, so a chord is folded on the pitches it lands on
(Green Hill's FM3 starts its grace a tick after FM4/FM5). A mixed (non-chip) pair needs no
equal durations: each sample plays out as it is.

| Follower at t | Result | Counted as |
|---|---|---|
| note-on, same duration | composite | `paired` |
| note-on, longer | composite; its tail is cut by the primary's next rest (`truncated`) or re-attacked by the primary's next note (`held` there) | `paired` |
| note-on, shorter | the primary alone: a composite cannot key one voice off early | `shorter` |
| none, resting | the primary alone (right) | `alone` |
| none, still sounding | the primary alone; the follower's ring is lost | `held` |
| note-on while the primary sounds, no primary note-on | lost; with the group's `cut_primary: true` it plays as a solo note and cuts the primary's tail | `orphan` / `cuts` |
| note-on while the primary is silent | the follower's own note, spliced into the primary's event stream with the follower's instrument, MOD note and level (`walk_channel` yields it with the follower's state); the follower's rest follows it unless the primary takes the channel back first | `solo` (`solo_cut` when a primary note-on ends it early) |

A pair is clean when nothing is lost; `tools/merge_survey.py` prints the counts for every
ordered pair of a song's channels and suggests groups (never one with orphans). Two channels
that never sound at once are a clean pair with nothing but solo notes: they simply share the
MOD channel. Follower notes whose modulation state differs from the primary's are counted
(`vibrato`) but play with the primary's `4xy`; a solo note carries no vibrato.

The followers stay in the walks of the merged build (`enabled_channels` yields them while
`merge_active`) so their instruments keep their baked levels, envelopes and rendering pitches,
which the composites and solo notes are made from; only `_convert_all_channels` skips them.
Instruments no note of the merged build plays are dropped from the catalogue before rendering
(`MergePlan.unused`, reported as "not rendered"); a sample a pcm composite is mixed from is
kept until the mix is done and blanked after.

**The fill pool** (`merge_fill: [PSG1, PSG2]`; a group's `fill_lost: true` for the follower
notes it cannot fold — its orphans and shorter notes, `PairStats.lost_notes`; a group's
`fill_cut: true` for the follower notes whose ring the fold would cut, `PairStats.cut_notes`:
a note longer than the primary's with the primary's next note-on or rest inside it) places
notes on ANY output channel that is silent when they start, not only their group's primary
(`_pool_notes`).  It runs before anything folds: a pooled follower note leaves its group's
notes and the groups are paired again, so a cut note that found a channel silent for all of
it plays whole there instead of as a truncated composite; one that found none folds as before
(reported as "stay folded").  Green Hill's lead rests through patterns 2–4, and PSG1's
24-tick chime rings, cut at 8 ticks inside the bass mixes, play whole on its channel there
(11 of 17).  The channels' occupancy counts the solo notes the groups will splice.    Each live channel's occupancy is its own notes'
sounding spans plus everything spliced onto it; a pool note takes the channel that stays silent
longest — the whole note where one can, else the channel whose next note-on cuts it
(`cut`), and never for less than a row (`fill_min_ticks`); a note with no silent channel is
lost.  `merge_fill_cut_after: {DAC: 2, FM2: 4}` lets a channel's notes count for that many
ticks only, so a pool note may cut a kick's decay or a bass note's second half — what a
hand-made 4-channel cover does.  Pool notes are spliced as solo notes are (`_splice_note`), so
they keep their own instrument, level and pitch on whatever channel they land, including the
drum channel (the FM level law applies to every FM note on a channel, spliced or not).  The
converter reports per source how many were placed where, cut, and lost (`merge_fill`).  Green
Hill's chime lines cannot be pooled: PSG2 starts with a bass note-on on 107 of its 113 notes and
PSG1 on 72 of 75, and every one of those also starts on a drum, so only 30 of 188 found a silent
channel even with `merge_fill_cut_after`; they fold onto the bass channel as bass+chime mixes
instead (`FM2 + [PSG2, PSG1]`, the survey's "folds with losses" pair).

**Composite instruments.** One per distinct key. Slots: the ones nothing in the config names,
then the ones the merged build frees (instruments no note plays once the followers are gone),
the most-played composites first (`_fit_composites`).  Each group's `max_composites` is
applied first; then, while the composites do not all fit, as many as are over are dropped —
first those whose primary instrument is played anyway (dropping them needs no new slot), then
the least played — and the fit is redone, because a dropped composite hands its notes back to
the primary's own instrument, which may be one of the slots on offer.  It used to be computed
once, before the budgets: a chord over budget fell back to FM5's instrument 11, which the unused
scan had already given away, and eight of Green Hill's notes played an empty slot.  The
converter prints `composite slots: N used of M free (K asked for)`; a dropped composite's notes
play the primary alone and `merge_unsupported` says which.  A composite owns the slot it is
given: both catalogues drop the instrument that used to be named there (`fm_catalogue` puts
the composite in over it, `psg_catalogue` pops every composite slot as well as `plan.unused`).
Until 2026-09-28 the FM catalogue kept the old owner and the PSG generator rendered its tone
into the slot regardless, so Green Hill's F+A+C chord (slot 18, once `psg_tone03`) played a
PSG chime and the composites in slots 13 and 15 played `ghz_v07` / `ghz_v08_hi`.

- two FM voices → `("fm", primary instrument, (follower voice, interval, detune, TL delta)...)`:
  an `FmInstrument` with one `FmLayer` per voice, added to the instrument catalogue and rendered
  by `ym2612.renderer.render_layers` — each layer on its own YM2612 channel at the composite's
  rendering pitch plus its interval, with the follower's `smpsDetune` (relative to the
  primary's) added to the frequency word as `FMUpdateFreq` does, and its carrier TL the
  follower's track level relative to the primary's (a hard pan counts 4 steps). The sample is
  rendered at the level the composite's own notes play most (`_plan_fm_render_levels` counts it
  like any instrument) and its `sample_list` volume is the primary's times the composite's
  peak over its primary layer's alone (the generator renders that layer by itself too), moved by
  the difference between the composite's and the primary instrument's baked levels: the primary
  plays as loud as it did and the follower adds to it as the hardware sum did.  Past 64 the
  volume is clamped and `merge_headroom` says by how much.
- anything else → `("pcm", primary instrument, primary MOD note, (follower instrument, follower
  MOD note, level gain)...)`: mixed by `mix_pcm_composites` once every sample is in. A MOD
  sample triggered at note n plays at `amiga_clock / PERIOD[n]` whatever rate it was made at, so
  every layer is resampled by the period ratio of its note and the composite's trigger note
  (the fastest layer's, so a hat on a kick keeps its treble; `MergePlan.note_at` gives the
  converter that note) and added at `sample_list volume × 10^((level − baked level)/20)`. The sum is
  peak-normalised and the composite's volume set to the sum's level; past full scale it stays
  at 64 and `merge_headroom` says by how much.  With sustain loops on, a looped follower is
  unrolled under the primary, and a looped primary mixed at its own rate keeps its loop, moved
  past the followers' tails (the unrolled data repeats the loop body, so any later repeat of it
  is the same seamless loop): Green Hill's bass+chime mixes are the chime's length plus one
  bass loop.  Mixed at another rate the loop points would not land on samples, so the primary
  is unrolled for its longest note instead and the mix plays straight through.

**When it runs.** The plan is built once the ticks are final (after `_apply_global_tempo_div`
and `_extend_looping_channels`, which now runs before anything counts notes) and before the
samples render, so the FM composites are catalogue entries like any other; it is stored on
`config.merge_plan`, which `walk_channel` reads, so the level pre-passes, the sustain scan and
`_convert_channel` all see the composite instruments the same way (the DAC branch asks the plan
directly).

**Verification.** The reference MOD is untouched by all this; the merged build is a second
regression case per song that has a `merge:` section (`<name>_merged`).
`tools/vgm_compare.py <config> <vgz> --merged` renders each merged MOD channel and, with a
combined mute mask, the sum of the chip channels folded onto it, and reports whole-song balance
and audio onsets per channel (the per-note audit needs one note stream per channel, so it is
the reference build's).  Title Screen after the volume rule above: every merged channel within
1.2 dB of its chip sum.

## SMPS Note Range to MOD Range

SMPS supports 8 octaves (C0–B7, bytes $81–$DF). MOD supports 3 octaves (C1–B3, 36 semitones). Mapping requires transposing down.

| SMPS note range | Bytes | Semitones | Recommended transpose | MOD result |
|-----------------|-------|-----------|----------------------|------------|
| C0–B2 (very low) | $81–$A8 | 0–35 | 0 | C1–B3 |
| C3–B5 (mid) | $A9–$C8 | 36–71 | **−36** | C1–B3 |
| C4–B6 (high) | $B9–$D8 | 48–83 | **−48** | C1–B3 (clips low) |
| C5–B7 (very high) | $C9–$DF | 60–94 | **−60** | C1–B3 (clips low+high) |

Notes outside C1–B3 after transpose are **clamped** (not silenced) with a warning. Use per-channel `transpose` in YAML and `voice_map` with `root` for best control.
