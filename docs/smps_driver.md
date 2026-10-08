# Sonic 1 SMPS Driver Reference

What the Sonic 1 sound driver (SMPS 68k Type 1b) and the two chips do with a song.  Sources:
`reference/smps_drivers/sonic_1/s1.sounddriver.asm`, `z80.asm`, `_smps2asm_inc.asm`.  How the
assembly is written and parsed: `docs/smps_format.md`.  What each effect becomes in a MOD:
`docs/pipeline.md` § SMPS → MOD mapping.

---

## Coordination flags ($E0–$F9)

A track byte below $80 is a duration, $80 a rest, $81–$DF a note (on the DAC track, a sample), $E0 and
up a flag (`CoordFlag` → `coordflagLookup`).

| Byte | Macro (alias) | Operands | What the driver does |
|------|---------------|----------|----------------------|
| $E0 | `smpsPan` | direction + AMS/FMS | Writes $B4: speakers and LFO sensitivity.  Ignored on PSG |
| $E1 | `smpsDetune` (`smpsAlterNote`) | signed | Sets `Detune`, added to every frequency write (§ Pitch) |
| $E2 | `smpsNop` | byte | Stores a byte the game can read; no sound |
| $E3 | `smpsReturn` | — | Returns from `smpsCall` |
| $E4 | `smpsFade` | — | Restores the music the 1-Up interrupted and fades it in; the 1-Up's track ends |
| $E5 | `smpsChanTempoDiv` | byte | This track's tempo divider |
| $E6 | `smpsAlterVol` | signed | Adds to the track's attenuation and rewrites the carrier TLs (`SendVoiceTL`) |
| $E7 | `smpsNoAttack` | — | Holds the next note (§ smpsNoAttack) |
| $E8 | `smpsNoteFill` | frames | Key-off timeout for every note from here (§ smpsNoteFill) |
| $E9 | `smpsChangeTransposition` (`smpsAlterPitch`) | signed | Adds semitones to `Transpose` (§ Pitch) |
| $EA | `smpsSetTempoMod` | byte | New tempo modifier, its counter restarted (§ Timing System) |
| $EB | `smpsSetTempoDiv` | byte | Every music track's tempo divider |
| $EC | `smpsPSGAlterVol` | signed | Adds to a PSG track's attenuation; heard from the next volume write |
| $ED | `smpsClearPush` | — | Lets the push-block SFX play again (SndA7) |
| $EE | `smpsStopSpecial` | — | Ends the track and hands FM4 back to the music (SndD0 Waterfall) |
| $EF | `smpsSetvoice` (`smpsFMvoice`) | voice | Loads an FM voice (`SetVoice`) |
| $F0 | `smpsModSet` | wait, speed, change, step | Sets and enables modulation (§ smpsModSet) |
| $F1 | `smpsModOn` | — | Enables modulation with the stored parameters (no Sonic 1 song uses it) |
| $F2 | `smpsStop` | — | Ends the track: FM key-off, PSG silenced |
| $F3 | `smpsPSGform` | byte | Turns the track into the noise channel (§ Noise) |
| $F4 | `smpsModOff` | — | Disables modulation |
| $F5 | `smpsPSGvoice` | `fTone_xx` | PSG volume envelope (§ PSG volume envelopes) |
| $F6 | `smpsJump` | offset | Jumps |
| $F7 | `smpsLoop` | slot, count, offset | Jumps back until the body has played `count` times |
| $F8 | `smpsCall` | offset | Pushes the return address and jumps |
| $F9 | `smpsMaxRelRate` (`smpsWeirdD1LRR`) | — | Writes $0F to FM1's $88 and $8C (D1L 0, RR 15 on two operators; Spring Yard) |

---

## Pitch

### Note tables

**FM.**  `FMSetFreq` takes `byte − $80 + Transpose`, masked to 7 bits, into `FMFrequencies`: eight
octaves of twelve words, each `block << 11 | fnum` (the $A4/$A0 register pair).  A row runs B to A♯
(fnum 606 … 1148) because index 0 is the rest, so `nC0` is index 1.  An FM label is the real
pitch at transposition 0: `nA4` = fnum 1084, block 4 = 440.5 Hz.

**PSG.**  `PSGSetFreq` takes `byte − $81 + Transpose`, masked to 7 bits, into `PSGFrequencies`:
70 SN76489 dividers.  Index *i* sounds C3 + *i* semitones (index 0 = 854 = 131 Hz), so a label
sounds three octaves above its name at transposition 0, one below with the usual header $D0.
The rows are transcribed Hz values, not exact octaves.  Index 69 is `nMaxPSG` (= `nA5`):
divider 0, which the PSG clocks as 1 (about 112 kHz, inaudible) — the noise channel's trigger
note.

The mask **wraps, it does not clamp**.  An index past the table reads the code that follows it:
indices 125–127 (a note one to three semitones below the table) were measured from the Spring
Yard and Credits rips as dividers 0, 922 and 540 (`PSG_FREQUENCIES_EXTENDED` in
`core/smps/driver_tables.py`, which transcribes both tables).

### smpsChangeTransposition ($E9) vs smpsDetune ($E1)

They are unrelated.  **Transposition** is whole semitones: the signed byte adds to `Transpose`
(the header's pitch byte is its starting value), cumulatively, and moves the table index of every
later note.  **Detune** is a signed byte added to the frequency word after the lookup, on every
write (`FMUpdateFreq`, `PSGUpdateFreq`), until changed.  On FM it is FNUM units: one unit is
1.5 c at A♯ (fnum 1148) to 2.9 c at B (606), so `$03` is +4.5 … 8.5 c — never a semitone, and the
note index never sees it.  On PSG it adds to the divider, so a positive detune **lowers** the pitch.
How the converter routes either: `docs/pipeline.md` § Notes: range, transpose, routing.

---

## Timing System

### Header

`smpsHeaderTempo divider, modifier`: the divider goes to every track, the modifier (*m*) is the main
tempo.  A duration byte is multiplied by the track's divider as it is read (`SetDuration`, a byte:
the product wraps at 256); `smpsChanTempoDiv` changes one track's divider, `smpsSetTempoDiv` every
track's, and the last write wins.

### TempoWait

Each V-int the main tempo counter counts down from *m*; when it runs out, `TempoWait` adds 1 to
every music track's `DurationTimeout` and reloads the counter.  So every *m*-th frame no track
advances:

```
ticks per second = fps × (m − 1) / m        m = 3: 40 (NTSC), 33.3 (PAL);  m = 5: 48 / 40
```

Ticks are unevenly spaced: tick *k* of a segment is read on frame `k + k // (m − 1)`
(`core/smps/tempo.py`).  `smpsSetTempoMod` writes the tempo and its counter, so the holds start
again from where it is read.  Only `DurationTimeout` is held: note fill, modulation and PSG
envelopes count every frame.  SFX tracks are never held — one tick a frame.  The BPM this gives a
MOD: `docs/pipeline.md` § Timing.

---

## Note reads

When a track's `DurationTimeout` runs out the driver clears `smpsNoAttack`, reads flags up to a
note or duration, and then:

- **FM** (`FMUpdateTrack`): `FMNoteOff` on the byte read (note, rest or duration), the frequency
  written, `FMNoteOn` unless resting.  Every read re-keys the channel.
- **PSG** (`PSGUpdateTrack`): `PSGDoNoteOn` writes the divider and `PSGDoVolFX` the volume — the
  key-on, after a fill or rest silenced the channel.
- Each read restarts the fill, the PSG envelope and the modulation (`FinishTrackUpdate`), unless
  `smpsNoAttack` is set.

A **duration byte with no note** takes the `.gotduration` path, skipping `FMSetFreq` /
`PSGSetFreq`: it re-keys at the frequency the track already holds — a transposition changed since
is not applied (SndA8 SS Goal).  After a rest that frequency is cleared (FM 0, PSG −1), so the
track keeps resting: Credits PSG3's `nRst, $24` followed by 32 loops of bare `$03, $03, $06` is
silent in the rip.  On the DAC track it re-hits the saved sample; after a rest, nothing.

### smpsNoAttack ($E7)

It skips the next note's key-off, not its key-on.  `FMNoteOn` still writes the key-on, which an
already keyed channel ignores: after a rest, or once `smpsNoteFill` keyed the note off, the note
attacks.  The fill, envelope and modulation carry on from the note before.  On FM the flag also
blocks a fill's key-off during the held note (`FMNoteOff` checks it); on PSG, `SetPSGVolume` writes
nothing while the flag is set and the fill has run out, so the held note stays silent.

---

## smpsModSet ($F0)

```asm
smpsModSet wait, speed, change, step     ; $F0 wait speed change step
```

| Operand | Field | Meaning |
|---------|-------|---------|
| `wait` | ModulationWait | Frames before modulation starts |
| `speed` | ModulationSpeed | Frames per step, reloaded from the data each step |
| `change` | ModulationDelta | Signed; added per step to the frequency word: FNUM on FM, divider on PSG |
| `step` | ModulationSteps | Steps per half-swing — **halved for the first half-swing only** |

`smpsModSet` and every attack store `step / 2` (`lsr.b #1`), but when the counter runs out
`DoModulation` reloads it from the **original** byte, negates the delta and spends that update.
With `$04`: 2 steps up, then 4 down, 4 up, … — a triangle of `delta·step/2` either side of the note
with a steady cycle of `2·speed·(step+1)` frames (`$00,$01,$06,$04`: 10 frames = 6 Hz, ±12 FNUM;
5.99 Hz measured on the Title Screen).  An odd `step` leaves the triangle half a delta off-centre.
No operand is multiplied by the tempo divider.  The `4xy` it becomes: `docs/pipeline.md` § Vibrato.

---

## smpsNoteFill ($E8)

`smpsNoteFill n` keys every note off `n` **frames** after it attacks (FM key-off, PSG silence);
0 turns it off.  It persists until changed: each attack reloads `NoteTimeout` from the stored
value.  The duration still decides when the next note starts.

- The fill counts frames, the duration ticks: with `smpsHeaderTempo $01,$05` a duration of 12
  lasts 250 ms, a fill of 12 lasts 200 ms (verified on the Title Screen VGZ).
- A fill equal to the duration fires when the modifier is > 1 (the note lasts
  `duration × m/(m−1)` frames).  Only with no hold frame in the note does the next read win the tie.
- The fill is never multiplied by the tempo divider.

How it becomes a cut: `docs/pipeline.md` § Note fill.

---

## FM voices

### Voice layout

25 bytes: `feedback << 3 | algorithm`, then six groups of four — DT/MUL, RS/AR, AM/D1R, D2R,
D1L/RR, TL — each group in SMPS operator order 4, 3, 2, 1 (the `smpsVc*` operands reversed).

### YM2612 register mapping

`SetVoice` writes each group to operator offsets `$00, $08, $04, $0C` (`FMInstrumentOperatorTable`),
so SMPS operator *n* is the chip's operator 5 − *n*:

```python
# core/smps/driver_tables.py — read by ym2612/voice.py and sfx/chips.py
SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)   # SMPS OP1..OP4 → chip OP4, OP3, OP2, OP1
```

The order `(0x00, 0x08, 0x04, 0x0C)` puts SMPS operator 1 — chip OP4, a carrier in every
algorithm — into OP1's feedback slot: an "overdriven guitar" distortion.

### Carriers by algorithm

| Algorithm | Carrier offsets | Chip operators |
|-----------|-----------------|----------------|
| 0–3 | $0C | OP4 |
| 4 | $08, $0C | OP2, OP4 |
| 5, 6 | $04, $08, $0C | OP2, OP3, OP4 |
| 7 | all | OP1–OP4 |

`FMSlotMask` names them.  `SetVoice` adds the track's attenuation (header volume + `smpsAlterVol`)
to the carrier TLs with `add.b` — modulo 256, the chip reading 7 bits.  `SendVoiceTL`, after
`smpsAlterVol`, rewrites the carriers only, skips one whose sum carries past $FF, and does nothing
while the attenuation is negative.  Several carriers at TL 0 overflow the chip's 9-bit channel
accumulator; that clipping is the hardware's (`docs/fm_synthesis.md`).

---

## DAC Channel

The Z80 plays DPCM samples through the YM2612's DAC (FM6).  The DAC track writes each sample byte
to `zDAC_Sample`; it has no key-off, fill or modulation, and a rest does not stop a sample: it
plays out.

### Sample table

| Constant | Byte | Sample | Driver pitch |
|----------|------|--------|--------------|
| `dKick` | $81 | kick | 23 |
| `dSnare` | $82 | snare | 1 |
| `dTimpani` | $83 | timpani | 27 |
| `dHiTimpani` | $88 | timpani | 18 |
| `dMidTimpani` | $89 | timpani | 21 |
| `dLowTimpani` | $8A | timpani | 28 |
| `dVLowTimpani` | $8B | timpani | 29 |

$88–$8B are the 68k's: it writes the pitch from `DAC_sample_rate` into the timpani's table entry
(`zTimpani_Pitch`) and plays $83.  The pitch stays, so a bare $83 afterwards plays at the last one
(no Sonic 1 song plays $83 itself).  $84–$86 are not samples; from $87 the Z80 plays the SEGA voice.

### DAC playback rates

The pitch is a loop counter.  Each sample's is computed from its source WAV's rate
(`dpcmLoopCounter`): kick 8,250 Hz, snare 24,000, timpani 7,375; the timpani variants scale that
rate by 1.30, 1.20, 0.97 and 0.95.

**What real hardware plays.**  `zPlayPCMLoop` takes exactly `301 + 26·(pitch − 1)` Z80 cycles a
byte (two samples) at 3,579,545 Hz — counted from the ROM's code (`core/rom/drivers/smps68k/dpcm.py`; `z80.asm`
matches it).  Wait states can only add to that.  On top of it the 68k stops the Z80 for the
whole music update once a frame (`UpdateMusic`'s `stopZ80`): the DAC holds for 3.8–6.4 % of the
time, by song.  The hardware rate is the cycle count less that share.

The VGZ rips are not the yardstick: their emulator runs the loop 1.7–2.8 % fast between the
stalls (snare 24,440 Hz against the count's 23,784), so their averaged rates land 2–3 % above the
hardware's, and `vgm_compare` reads the DAC 1–3 % flat against a rip.

| Sample | Pitch | Cycle count | Hardware at a 5 % stall | MOD note |
|--------|-------|-------------|-------------------------|----------|
| dKick | 23 | 8,201 Hz | ~7,790 Hz | B1 |
| dSnare | 1 | 23,784 Hz | ~22,590 Hz | F3 |
| dTimpani | 27 | 7,328 Hz | ~6,960 Hz | A1 |
| dHiTimpani | 18 | 9,635 Hz | ~9,150 Hz | D2 |
| dMidTimpani | 21 | 8,720 Hz | ~8,280 Hz | C2 |
| dLowTimpani | 28 | 7,138 Hz | ~6,780 Hz | A1 |
| dVLowTimpani | 29 | 6,957 Hz | ~6,610 Hz | G#1 |

Choosing a config's note and finetune from these: `docs/yaml_config.md`.

---

## PSG Channels

Three SN76489 tone channels (PSG1–PSG3) and a noise channel, which PSG3's track drives once it is
a noise track.  Pitch: § Note tables.  The synthesis: `docs/psg_synthesis.md`.

### PSG volume envelopes

`smpsPSGvoice fTone_0n` (or the header's voice) selects envelope `PSGn` of nine; 0 is none.  From
each attack one step a frame is added to the track's attenuation (header volume +
`smpsPSGAlterVol`), capped at $F (silence).  The `$80` terminator holds: `VolEnvHold` steps the
index back and writes nothing, so the last value stays.  An envelope shapes the amplitude only.

### Noise

`smpsPSGform` (`cfSetPSGNoise`) sets the track's `VoiceControl` to $E0 for good — no flag turns it
back — and writes its byte to the noise register: `$E0 | white << 2 | rate`.  Rates 0–2 are fixed
clocks; rate 3 clocks the LFSR from tone channel 3, whose divider the track's notes now write.
Every Sonic 1 noise track uses `$E7` (white, rate 3): `nMaxPSG` gives the fastest hiss, other
notes pitch the noise.  `smpsPSGvoice` afterwards changes only the envelope.
