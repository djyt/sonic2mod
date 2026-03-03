# sonic2mod Conversion Pipeline

How SMPS assembly music maps to Amiga ProTracker MOD format.

Related docs: `docs/smps_driver.md` (driver internals), `docs/smps_format.md` (assembly syntax),
`docs/architecture.md` (module overview), `docs/effects.txt` (ProTracker effect reference),
`docs/synthesis.md` (FM synthesis — synth_root, pitch matching, OPN2 internals),
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
      │  5. For each configured channel: walk SmpsEvent list → write MOD rows
      │  6. Set song loop (Bxx) from first smpsJump event found
      │
      ▼
  ModFile → mod.get_bytes()        mod.py
      │
      ▼
  .mod binary
```

---

## SMPS → MOD Effect Mapping

One effect per note-row in MOD format. See `docs/effects.txt` for full ProTracker effect documentation.

| SMPS Command | Byte | Parameters | MOD Effect | MOD Code | Notes |
|-------------|------|------------|------------|----------|-------|
| `smpsAlterVol` | $E6 | signed delta | Set Volume | `Cxx` (Cmd C) | Cumulative; emitted only when volume changes from last |
| `smpsModSet` | $F0 | wait,speed,change,step | Vibrato | `4xy` (Cmd 4) | x=speed nibble, y=change nibble; approximate (triangle→sine) |
| `smpsModOn` | $F1 | — | Vibrato (continues) | `4xy` | Re-activates stored params |
| `smpsModOff` | $F4 | — | (clears vibrato state) | none | No MOD effect; future notes have no vibrato |
| `smpsNoteFill` | $E8 | byte 1–15 | Note Cut | `ECx` (Cmd EC) | x = fill ticks (4-bit); values > 15 not representable |
| `smpsJump` | $F6 | address | Position Jump | `Bxx` (Cmd B) | xx = target pattern position; first occurrence per song only |
| `smpsLoop` | $F7 | idx,count,addr | (none — unrolled) | — | Loop body replayed at parse time |
| `smpsCall` | $F8 | address | (none — inlined) | — | Subroutine events spliced into caller |
| `smpsSetvoice` | $EF | voice index | (instrument routing) | — | Updates voice_map lookup; no direct MOD effect |
| `smpsChangeTransposition` | $E9 | signed byte | (pitch shift) | — | Updates total_transpose; affects next note placement |
| `smpsDetune` / `smpsAlterNote` | $E1 | signed byte | (none) | — | FNUM offset (~10 cents); not applied to MOD pitch |
| `smpsPan` | $E0 | direction | (none) | — | MOD panning is channel-based; ignored |
| `smpsNoAttack` | $E7 | — | (flagged on note) | — | No MOD equivalent; note plays without re-attack in SMPS |
| `smpsNop` | $E2 | byte | (none) | — | Game sync byte; ignored |
| `smpsPSGform` | $F3 | byte | (none) | — | PSG waveform; ignored |
| `smpsPSGvoice` | $F5 | byte | (none) | — | PSG envelope; ignored |
| `smpsMaxRelRate` | $F9 | — | (none) | — | FM1 release; ignored |

### Effect priority (one per note-row)

When multiple effects are active on the same note, **first match wins**:

1. **Cxx — Set Volume** (`smpsAlterVol` result differs from current): volume changes take priority because they affect all subsequent notes until changed again.
2. **4xy — Vibrato** (`smpsModSet/On` active, speed > 0): vibrato is a continuous effect; priority over note-cut.
3. **ECx — Note Cut** (`smpsNoteFill` set, fill ≤ 15): lowest priority.

Implication: if volume changes on the same row as vibrato, vibrato is dropped for that row. Design songs (and YAML configs) to avoid stacking these on the same row.

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

### 3. smpsNoteFill > 15 is silently ignored

**Problem:** Note fills specified as `$10` or higher produce no MOD note-cut effect.

**Cause:** MOD command `ECx` has a 4-bit parameter (0–15 ticks). `smps2mod.py` checks `0 < note_fill < 0x10` before emitting ECx.

**Fix:** This is not a bug — fill values > 15 mean "note holds for more than 15 ticks" which is naturally represented by the note's duration. No workaround needed.

---

### 4. smpsModSet step count is halved in hardware

**Problem:** Vibrato seems shallower than expected.

**Cause:** The Sonic 1 driver halves the `step` parameter before storing it (`lsr.b #1`). `step=16` → 8 actual oscillation steps.

**Fix:** MOD vibrato (`4xy`) has different semantics (sinusoidal, not triangle). Treat the translation as approximate. Tune `4xy` values manually in the tracker if needed.

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

### 7. Song loop uses only the first smpsJump

**Problem:** Loop point seems to come from the wrong channel.

**Cause:** `_set_loop_point()` in `smps2mod.py` uses the position from the first channel that contains an `smpsJump` event. All well-formed Sonic 1 songs loop all channels simultaneously at the same position.

**Fix:** Verify in the `.asm` source that `smpsJump` labels are consistent across channels. This is not configurable.

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

**Cause:** A duration byte with no preceding note on the same `dc.b` line is a **sustain/wait** command — it advances the tick counter while the previous note continues. The parser emits an implicit continuation event (`is_rest=True, is_no_attack=True`). This is correct per the SMPS spec.

**Fix:** No fix needed — this is working as designed. The implicit wait correctly represents the held note duration.

---

## SMPS Note Range to MOD Range

SMPS supports 8 octaves (C0–B7, bytes $81–$DF). MOD supports 3 octaves (C1–B3, 36 semitones). Mapping requires transposing down.

| SMPS note range | Bytes | Semitones | Recommended transpose | MOD result |
|-----------------|-------|-----------|----------------------|------------|
| C0–B2 (very low) | $81–$A8 | 0–35 | 0 | C1–B3 |
| C3–B5 (mid) | $A9–$C8 | 36–71 | **−36** | C1–B3 |
| C4–B6 (high) | $B9–$D8 | 48–83 | **−48** | C1–B3 (clips low) |
| C5–B7 (very high) | $C9–$DF | 60–94 | **−60** | C1–B3 (clips low+high) |

Notes outside C1–B3 after transpose are **clamped** (not silenced) with a warning. Use per-channel `transpose` in YAML and `voice_map` with `root` for best control.
