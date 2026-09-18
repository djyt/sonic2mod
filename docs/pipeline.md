# sonic2mod Conversion Pipeline

How SMPS assembly music maps to Amiga ProTracker MOD format.

Related docs: `docs/smps_driver.md` (driver internals), `docs/smps_format.md` (assembly syntax),
`docs/architecture.md` (module overview), `docs/mod_effects.txt` (ProTracker effect reference),
`docs/synthesis.md` (FM synthesis — synth_root, pitch matching, OPN2 internals),
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
| `smpsModSet` | $F0 | wait,speed,change,step | Vibrato | `4xy` (Cmd 4) | x=speed nibble, y=change nibble; approximate (triangle→sine) |
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

### FM levels (`fm_volume_scaling: baked`)

On the chip an FM note's level is set by the track's TL offset — `smpsHeaderFM` volume plus every
`smpsAlterVol` so far, 0.75 dB per step — and by its pan: a centred channel drives both speakers,
a hard-panned one drives one (−3 dB power).  A MOD note's level is its instrument's default
volume unless a `Cxx` overrides it, and instruments cannot share sample data, so a second copy of
a sample at another volume costs its full size.

`SmpsToModConverter._plan_fm_levels` therefore walks the FM channels first and, for every MOD
instrument, counts notes per level `−0.75 × TL − pan`.  The level with the most notes is that
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
`smpsPSGAlterVol`, 15 = silent), planned by `_plan_psg_levels` with the converter's own instrument
tracking (header voice, `smpsPSGform` → `psg_map`, `smpsPSGvoice` → `psg_voice_map` and its
per-note range dispatch).  The attenuation most of an instrument's notes play at needs no command
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
| Sample size | 65534 bytes (words × 2) |
| Note range | C1–B3 (36 semitones) |

---

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
output_note = entry.root.value + (source_semitone − entry.low)
```

- `root` is **unconditional** — `smpsDetune`, header pitch_offset, and `smpsChangeTransposition` do NOT affect the root path.
- Ensure `root + (high − low)` stays within C1–B3 (values 0–35) to avoid clamping.

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

### 4. smpsModSet step count is halved in hardware

**Problem:** Vibrato seems shallower than expected.

**Cause:** The Sonic 1 driver halves the `step` parameter before storing it (`lsr.b #1`). `step=16` → 8 actual oscillation steps.

**Fix:** MOD vibrato (`4xy`) has different semantics (sinusoidal, not triangle). Treat the translation as approximate. Tune `4xy` values manually in the tracker if needed.

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

**Fix (done):** `SmpsToModConverter._ticks_per_frame` = `(mod−1)/mod` (1.0 for SFX / mod ≤ 1)
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

**Not covered:** mid-song `smpsSetTempoMod` (Credits, Drowning) — the header modifier is used
throughout.  The `smpsModSet` *speed* → `4xy` rate formula is a separate issue (gotcha 4).

---

### 5. Wrong operator order → distorted FM synthesis

**Problem:** Synthesized FM samples sound like an overdriven guitar / extreme distortion.

**Cause:** Wrong `_SMPS_OP_TO_REG_OFFSET` mapping in `ym2612/voice.py`. SMPS stores operators in reversed order (OP4,OP3,OP2,OP1); the correct mapping is `(0x0C, 0x04, 0x08, 0x00)`. The wrong mapping `(0x00, 0x08, 0x04, 0x0C)` puts OP1 (often TL≈$01, near max volume) into the self-feedback slot.

**Fix:** Verify `_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)` in `ym2612/voice.py`. Do not change it.

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
(theory 6.0 Hz), MOD `485` 3.98 Hz ±36 c.

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
`MOD-only` counts rows the chip has no key-on for (a re-trigger where the hardware ties).  Title
Screen: 0 unmatched on every FM channel; GHZ: FM1 4, FM3 2, FM4 15, FM5 15 — the grace notes of
todo item 3.  The 40 ms window follows the running deviation of the notes matched so far, and the
change from the song's first notes to its last is printed as `drift` when it reaches 20 ms: a
MOD that runs slightly off the driver's tempo is one finding, not a lost note per bar (Special
Stage: +130 ms over its 33 s pass, 0 unmatched).  An audio onset detector cannot do this on sustained channels (it read 28–143
unmatched per GHZ channel with every note in place).  The DAC still uses it — the log holds PCM
seeks, not hits (GHZ has two seeks 20 ms apart and hits with none) — so its count stays
approximate (Title Screen 3, GHZ 60).

## SMPS Note Range to MOD Range

SMPS supports 8 octaves (C0–B7, bytes $81–$DF). MOD supports 3 octaves (C1–B3, 36 semitones). Mapping requires transposing down.

| SMPS note range | Bytes | Semitones | Recommended transpose | MOD result |
|-----------------|-------|-----------|----------------------|------------|
| C0–B2 (very low) | $81–$A8 | 0–35 | 0 | C1–B3 |
| C3–B5 (mid) | $A9–$C8 | 36–71 | **−36** | C1–B3 |
| C4–B6 (high) | $B9–$D8 | 48–83 | **−48** | C1–B3 (clips low) |
| C5–B7 (very high) | $C9–$DF | 60–94 | **−60** | C1–B3 (clips low+high) |

Notes outside C1–B3 after transpose are **clamped** (not silenced) with a warning. Use per-channel `transpose` in YAML and `voice_map` with `root` for best control.
