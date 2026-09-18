# Sonic 1 SMPS Driver Reference

Technical reference for the Sonic 1 68k sound driver as relevant to sonic2mod.
Sources: `sonic_1/s1.sounddriver.asm`, `sonic_1/_smps2asm_inc.asm`.

See also: `docs/smps_format.md` (assembly syntax), `docs/pipeline.md` (conversion pipeline).

---

## Coordination Flag Table ($E0–$F9)

All bytes ≥ $E0 in channel data are coordination flags (effect commands). Bytes < $80 are durations, $80 = rest, $81–$DF = notes.

| Byte | Macro (canonical) | Alias | Parameters | Hardware Effect | sonic2mod |
|------|-------------------|-------|------------|-----------------|-----------|
| $E0 | `smpsPan` | — | direction+amsfms | Set YM2612 panning (L/R/centre) + AMS/FMS LFO bits | parsed, informational only |
| $E1 | `smpsDetune` | `smpsAlterNote` | signed byte | **FNUM offset** (raw frequency count, ~10 cents/unit) added to SMPS_Track.Detune | parsed, stored; NOT applied to pitch or range lookup |
| $E2 | `smpsNop` | — | byte | Write game-sync flag to shared RAM; no audio effect | parsed, ignored |
| $E3 | `smpsReturn` | — | — | Return from `smpsCall` subroutine (S1/S2 drivers) | terminates inline |
| $E4 | `smpsFade` | — | — | Fade in previous song (1-Up jingle mechanism) | ignored |
| $E5 | `smpsChanTempoDiv` | — | byte | Per-channel tempo divider | parsed; per-channel divider applied to note durations at parse time |
| $E6 | `smpsAlterVol` | — | signed byte | Add delta to SMPS_Track.Volume attenuation (cumulative) | → `Cxx` Set Volume |
| $E7 | `smpsNoAttack` | — | — | Suppress attack envelope on next note | flagged on note |
| $E8 | `smpsNoteFill` | — | byte | Set note-cut timeout (SMPS_Track.NoteTimeout) in **frames** | → `ECx` / `C00` Note Cut |
| $E9 | `smpsChangeTransposition` | `smpsAlterPitch` | signed byte | **Semitone shift** — add to SMPS_Track.Transpose; all subsequent notes pitched accordingly | → updates `total_transpose`; affects note placement |
| $EA | `smpsSetTempoMod` | — | byte | Set global tempo modifier | ignored |
| $EB | `smpsSetTempoDiv` | — | byte | Set global tempo divider | ignored |
| $EC | `smpsPSGAlterVol` | — | signed byte | PSG volume attenuation delta | → `Cxx` Set Volume (same as smpsAlterVol) |
| $ED | (S1 specific) | — | — | Clear "push block" sound flag | ignored |
| $EE | `smpsStopSpecial` | — | — | Stop special SFX, resume interrupted music track | ignored |
| $EF | `smpsFMvoice` | `smpsSetvoice` | voice index | Load FM voice at index into YM2612 registers | → instrument routing via `voice_map` |
| $F0 | `smpsModSet` | — | wait, speed, change, step | Set modulation (vibrato) parameters; enables modulation flag | → `4xy` Vibrato |
| $F1 | `smpsModOn` | — | — | Re-enable modulation (uses stored params) | → activates `4xy` |
| $F2 | `smpsStop` | — | — | End of channel data | terminates parsing |
| $F3 | `smpsPSGform` | — | byte | Set PSG noise/waveform register | → instrument switch via `psg_map` config |
| $F4 | `smpsModOff` | — | — | Disable modulation | → clears vibrato state |
| $F5 | `smpsPSGvoice` | — | label | Set PSG tone envelope index | → instrument switch via `psg_voice_map` config |
| $F6 | `smpsJump` | — | address | Unconditional jump (song loop point) | → `Bxx` Position Jump (first occurrence only) |
| $F7 | `smpsLoop` | — | index, count, address | Loop back to address count times | unrolled at parse time |
| $F8 | `smpsCall` | — | address | Call subroutine at address | inlined at parse time |
| $F9 | `smpsMaxRelRate` | — | — | Set D1L+RR to max for FM1 ops 3&4 | ignored |

> **Note:** `smpsAlterNote` is a **backwards-compatibility alias** for `smpsDetune` (`$E1`). Similarly, `smpsAlterPitch` is an alias for `smpsChangeTransposition` (`$E9`). These are the same hardware commands.

---

## smpsDetune ($E1) vs smpsChangeTransposition ($E9)

This is the single most common source of confusion. They are fundamentally different.

### smpsDetune / smpsAlterNote ($E1) — FNUM offset

```asm
; In s1.sounddriver.asm (cfDetune handler):
;   add.w d0, d6    ; d6 = FNUM; d0 = detune value
;   → raw 11-bit FNUM register adjusted directly
```

- The 11-bit FNUM value controls YM2612 pitch within the current block (octave).
- At FNUM ≈ 720 (C4), adding 3 yields FNUM = 723 ≈ +10 cents (logarithmic).
- **Effect is sub-semitone** — cannot shift by a full semitone or change octave.
- Stored in `SMPS_Track.Detune`.
- **sonic2mod**: stored in `alter_note` field; explicitly NOT applied to semitone calculation or `voice_map` range lookup. Used only for chorus-style detuning.
- Typical values: `$02`–`$04` (detuned unison for chorus). Larger values cause obvious pitch drift.

### smpsChangeTransposition / smpsAlterPitch ($E9) — semitone shift

```asm
; In s1.sounddriver.asm (cfChangeTransposition handler):
;   add.b d0, SMPS_Track.Transpose(a5)  ; d0 = signed delta
;   → YM note table lookup uses Transpose as semitone offset
```

- Adds to `SMPS_Track.Transpose`. Cumulative — multiple calls stack.
- **Effect is whole semitones** — can span multiple octaves.
- Affects every subsequent note until changed again.
- **sonic2mod**: updates `transpose` in the channel state. Included in `total_transpose`, which determines the final MOD note position. If a `voice_map` entry has no `root`, `total_transpose` controls pitch.
- Common pattern in GHZ: FM channels shift by -24 or +24 semitones mid-song to reach different register ranges.

### Decision guide for voice_map

| Condition | Use |
|-----------|-----|
| Channel never uses $E9 | `root` safe — pitch is static |
| Channel uses $E9 mid-song | Omit `root`; use `transpose` + `total_transpose` path |
| Channel uses $E1 (detune) for chorus | Route to finetune variant instrument via `channel_instrument_map` |

---

## Timing System

### Header

```asm
smpsHeaderTempo divider, modifier
```

- **`divider`**: Multiplies all raw duration bytes from the assembly at load time. Usually `$01` in Sonic 1 (no scaling). If `$06`, a `dc.b $01` note lasts 6 driver frames.
- **`modifier`**: Frequency of the TempoWait interrupt. A value of `$05` means TempoWait fires every 5 frames.

### TempoWait mechanism

Each VBlank (60 Hz NTSC, 50 Hz PAL):
1. Decrement the main tempo counter.
2. If counter = 0: fire TempoWait — **add 1 to every track's DurationTimeout**, reset counter to `modifier`.
3. Decrement every track's DurationTimeout. When 0: advance to next note event.

Net effect: every `modifier` frames, one decrement is cancelled. Effective tick rate:

```
effective_ticks_per_sec = fps × (modifier − 1) / modifier
```

| modifier | NTSC eff. rate | PAL eff. rate |
|----------|---------------|---------------|
| 3        | 40 ticks/sec  | 33.3 ticks/sec |
| 5        | 48 ticks/sec  | 40 ticks/sec  |
| 7        | 51.4 ticks/sec| 42.9 ticks/sec |

### BPM formula

```
BPM = fps × (modifier − 1) × speed × 2.5
      ─────────────────────────────────────
      modifier × divider × ticks_per_row
```

Where `speed` = MOD ticks-per-row (target_speed in YAML) and `ticks_per_row` = SMPS ticks per MOD row.

When `speed == ticks_per_row` (simplest case):

```
BPM = fps × (modifier − 1) × 2.5 / (modifier × divider)
```

**Worked examples:**

| Song | divider | modifier | fps | speed | tpr | BPM |
|------|---------|----------|-----|-------|-----|-----|
| Title Screen | 1 | 5 | 60 | 6 | 6 | **120** |
| GHZ | 1 | 3 | 60 | 3 | 2 | **150** |

Use `auto_bpm: true` in YAML to compute this automatically.

### Choosing ticks_per_row

`ticks_per_row` controls how many SMPS ticks map to one MOD row. Set it to the GCD of the note durations that appear in the song.

| Common durations | GCD | ticks_per_row |
|-----------------|-----|---------------|
| $06, $0C, $18 | 6 | 6 |
| $04, $08, $0C | 4 | 4 |
| $04, $06, $0C | 2 | 2 |

---

## smpsModSet ($F0) — Vibrato Parameters

```asm
smpsModSet wait, speed, change, step
; Emits: $F0 wait speed change step
```

| Parameter | Field | Meaning |
|-----------|-------|---------|
| `wait` | ModulationWait | V-int **frames** before modulation starts (not tempo ticks) |
| `speed` | ModulationSpeed | **Frames** per step; reloaded from the data each step, never multiplied by the tempo divider |
| `change` | ModulationDelta | Signed; added per step to the note's frequency word — FNUM on FM, SN76489 divider on PSG |
| `step` | ModulationSteps | Steps per half-swing — **halved for the first half-swing only** |

> **Hardware quirk:** `smpsModSet` and every note start store `step / 2` (`lsr.b #1`), but when the
> counter runs out `DoModulation` reloads it from the **original** byte (`move.b 3(a0),…`), negates
> the delta and spends that update.  So with `$04`: 2 steps up, then 4 down, 4 up, … — a triangle
> of `delta·step/2` either side of centre with a steady cycle of `2·speed·(step+1)` frames
> (`$00,$01,$06,$04` → 10 frames = 6 Hz, ±12 FNUM; measured 5.99 Hz on the Title Screen).
> An odd `step` leaves the triangle half a delta off-centre.

**MOD mapping:** `4xy` Vibrato; x is derived from the cycle length and y per note from the swing
relative to the note's FNUM / divider — formulas and measurements in `docs/pipeline.md` gotcha 4.
MOD vibrato is sinusoidal where SMPS modulation is a triangle; peaks are matched.

**smpsModOn ($F1):** Re-enables modulation using the most recently stored ModSet parameters.
**smpsModOff ($F4):** Disables modulation. Next note will not vibrate.

---

## smpsNoteFill ($E8) — Note Cut Timeout

```asm
smpsNoteFill $0A   ; note silences after 10 frames (V-ints)
```

- Sets `SMPS_Track.NoteTimeout` to the fill value.
- Each driver frame: decrement NoteTimeout. When 0 → key-off (YM2612 release; PSG silence).
- **Frames, not tempo ticks.** `TempoWait` only bumps `DurationTimeout`; `NoteTimeoutUpdate` (and
  `DoModulation`) still run on the skipped frame. With `smpsHeaderTempo $01,$05` a duration byte
  of 12 lasts 250 ms but a fill of 12 lasts 200 ms. Verified on the Title Screen VGZ
  (`docs/audits/01_title_screen_audit.md`).
- **NoteTimeout and duration run in parallel.** Duration controls when the *next note starts*; NoteTimeout controls when the *current note silences*.
- `NoteTimeout` is reset to `NoteTimeoutMaster` (the last-set fill value) on every new note, even if `smpsNoteFill` is not repeated. The fill value persists until changed.

- **A fill equal to the duration byte still fires** when the tempo modifier is > 1: the note lasts
  `duration × mod/(mod−1)` frames, the fill exactly `fill` frames.  (Only with no TempoWait frames
  in the span does DurationTimeout win the tie.)
- The fill byte is **not** multiplied by the tempo divider (`cfNoteTimeout` stores it raw;
  `SetDuration` multiplies durations only).  Same for `ModulationWait` / `ModulationSpeed`.

**sonic2mod mapping:** the fill is scaled to ticks (`× (mod−1)/mod`) and placed to the MOD tick —
`ECx` inside a row, `C00` on a row boundary, on whichever row of the note it falls.  No cut when
the fill outlasts the note.  See `docs/pipeline.md` gotchas 3 and 4b.

---

## FM Voice Format

### SMPS binary layout (in-memory, as stored in .asm files)

Operators are stored in **reversed order** compared to YM2612 hardware registers:

```
SMPS byte stream:  [Algorithm+Feedback] [OP4 params...] [OP3 params...] [OP2 params...] [OP1 params...]
```

Each operator block (6 parameter bytes) uses this per-register order:
```
(DT<<4)|CF,  (RS<<6)|AR,  AM|D1R,  D2R,  (DL<<4)|RR,  TL
```

### YM2612 register mapping (sonic2mod)

```python
_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)
# SMPS OP1 → YM offset 0x0C
# SMPS OP2 → YM offset 0x04
# SMPS OP3 → YM offset 0x08
# SMPS OP4 → YM offset 0x00
```

The S1 driver writes them to hardware offsets `0x00, 0x08, 0x04, 0x0C` from the FMInstrumentOperatorTable (reversed from SMPS storage), resulting in the mapping above.

> **Critical gotcha:** Using `(0x00, 0x08, 0x04, 0x0C)` instead places OP1 (often TL≈$01, near full volume) into the self-feedback slot, producing severe distortion ("overdriven guitar" sound).

### Carrier operators by algorithm

Only carrier operators produce audio output. Carrier TL controls output volume.

| Algorithm | Carriers (YM offsets) | Topology |
|-----------|----------------------|----------|
| 0 | 0x0C (OP4) | Single carrier; 3 modulators in series |
| 1 | 0x0C (OP4) | Two-op parallel modulator + carrier |
| 2 | 0x0C (OP4) | Three-op modulator + carrier |
| 3 | 0x0C (OP4) | Two parallel + one carrier |
| 4 | 0x04, 0x0C (OP2+OP4) | Two carriers; OP1→OP2, OP3→OP4 |
| 5 | 0x04, 0x08, 0x0C (OP2+OP3+OP4) | Three carriers; OP1 modulates all |
| 6 | 0x04, 0x08, 0x0C (OP2+OP3+OP4) | Three carriers + OP1 self-feedback |
| 7 | 0x00, 0x04, 0x08, 0x0C (all) | Four carriers; pure additive synthesis |

Algorithms 4–7 with TL=0 carriers are prone to clipping in synthesis. See `headroom_db` and `carrier_balance` in `configs/settings.yaml`.

### smpsVcAmpMod note (SMPS2ASM version difference)

In older SMPS2ASM (version 0): AM bit is stored in bits 6–7. In SMPS2ASM v1+: AM bit is the high bit (bit 7). sonic2mod reads raw bytes; the AM bit position depends on which assembler version created the file.

---

## DAC Channel

The DAC channel (driven by the Z80) plays PCM samples via the YM2612 DAC port.

### Sample table (Sonic 1)

| Constant | Byte | Sample |
|----------|------|--------|
| `dKick` | $81 | Kick drum (~8,250 Hz) |
| `dSnare` | $82 | Snare (~24,000 Hz) |
| `dTimpani` | $83 | Timpani (~7,375 Hz) |
| `dHiTimpani` | $88 | Timpani × 1.30 (~9,588 Hz) |
| `dMidTimpani` | $89 | Timpani × 1.20 (~8,850 Hz) |
| `dLowTimpani` | $8A | Timpani × 0.97 (~7,154 Hz) |
| `dVLowTimpani` | $8B | Timpani × 0.95 (~7,006 Hz) |

Gaps ($84–$87) are unused in Sonic 1. The Z80 firmware reads the sample ID from shared RAM (`zDAC_Sample`) and plays it at the rate from its internal rate table.

### Channel data format

DAC data uses the same `dc.b` stream as FM/PSG channels:
- Bytes < $80: duration in ticks
- Bytes ≥ $80: sample ID (written to Z80 shared RAM)
- Duration carries forward (same persistence rules as FM/PSG)

The DAC channel does not support voice switching (`smpsSetvoice`) or modulation.

---

## PSG Channels

See also: `docs/psg_synthesis.md` (SN76489 synthesis pipeline, envelope tables, psg_map schema).

Three SN76489 square-wave generators (PSG1–PSG3) plus a noise channel.

### Note range

PSG notes use the same byte range as FM ($81–$DF), but the driver applies a different frequency table (SN76489 uses a period register, not FNUM/block). `nMaxPSG` = `nA5` ($C6) in Sonic 1 — this is the highest frequency the SN76489 can reliably produce.

### PSG volume envelopes

PSG channels use `fTone_01`–`fTone_09` (Sonic 1 has 9 envelopes). Set via `smpsPSGvoice`. These control amplitude shape (attack/decay), not timbre.

### PSG3 / Noise

The third PSG channel can drive the SN76489 noise register via `smpsPSGform`:
- `smpsPSGform $E7`: white noise at fixed rate
- Other values: periodic noise or noise locked to PSG3 tone frequency

In Sonic 1 songs, PSG3 typically plays a noise-based rhythm pattern using `nMaxPSG` as the trigger note and `smpsNoteFill` for note-cut timing.

### PSGUpdateTrack retrigger on every DurationTimeout

`PSGUpdateTrack` structure on every driver frame:

```
subq.b #1, DurationTimeout
bne   .notegoing          ; still counting → go to .notegoing
; ─── DurationTimeout expired ───
bclr  #4                  ; clear some flag
jsr   PSGDoNext           ; consume next data byte (note OR standalone duration)
jsr   PSGDoNoteOn         ; write frequency to SN76489 (uses stored Freq)
bra   PSGDoVolFX          ; restore volume (key-on if previously silenced)
.notegoing:
  ; NoteTimeoutUpdate, PSGUpdateVolFX, DoModulation, PSGUpdateFreq
```

**Key implication:** `PSGDoNoteOn` and `PSGDoVolFX` are called unconditionally after *every*
`DurationTimeout` expiry, regardless of whether `PSGDoNext` read a note byte or a duration byte.
For PSG noise, `PSGDoVolFX → SetPSGVolume` restores the channel volume from `$FF` (set by the
preceding `PSGNoteOff`) back to the audible level — this is a full key-on / retrigger.

This means **standalone `dc.b $XX` duration bytes produce real note retriggers**, not waits.
See `docs/smps_format.md` § Standalone Duration Bytes for the format-level description.

### smpsAlterNote on PSG

`smpsAlterNote` on PSG channels adds to the SN76489 period counter, similar to YM2612 FNUM offset. Effect is sub-semitone detune — less commonly used than on FM.
