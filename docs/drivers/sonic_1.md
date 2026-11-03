# Sonic 1: the SMPS 68k driver

What Sonic the Hedgehog's sound driver (SMPS 68k Type 1b, modified) and the two chips do with a song:
the coordination flags, pitch, timing, note reads, modulation, note fill, FM voices, the DAC and the
PSG.  Every other driver is described by how it differs from this one.  For anyone reading a song's
bytes or checking why the converter plays a note as it does.

Sources: the disassembly's `reference/smps_drivers/sonic_1/s1.sounddriver.asm`, `z80.asm`,
`_smps2asm_inc.asm` (gitignored).  How the assembly is written and parsed: [smps_asm.md](smps_asm.md).
What each effect becomes in a MOD: [../conversion/effects.md](../conversion/effects.md).

On this page:
- [Where things are](#where-things-are)
- [Coordination flags](#coordination-flags)
- [Pitch](#pitch)
- [Timing](#timing)
- [Note reads](#note-reads)
- [Modulation (smpsModSet)](#modulation-smpsmodset)
- [Note fill (smpsNoteFill)](#note-fill-smpsnotefill)
- [FM voices](#fm-voices)
- [DAC](#dac)
- [PSG](#psg)
- [How it converts](#how-it-converts)

## Where things are

| What | Where | Note |
|------|-------|------|
| Driver | `core/drivers/smps68k/sonic1/` | `SONIC1` (`variant.py`); tables and `SONIC1_RULES` in `core/drivers/reference.py` |
| ROM (Rev 01) | `input/roms/sonic_rev01.bin` | SHA-1 `1f1e480f…` |
| `Go_` block | ROM `$71990` | six longs: priorities, special SFX, music, SFX, speed-up, `PSG_Index` |
| Music index | ROM `$71A9C` | songs `$81`-`$93`; SFX from `$A0`, special SFX from `$D0` |
| PSG envelopes | ROM `$719A8` (`PSG_Index`) | nine |
| Songs as asm | `reference/smps_drivers/sonic_1/music/`, `input/sonic_1/` | [smps_asm.md](smps_asm.md) |
| Configs | `configs/sonic_1/` | 19 full configs (channels stated); 7 with a `lofi` variant |

`core/drivers/smps68k/locate.py` recognises an SMPS 68k driver by Sonic 1's first twelve FM frequency
words and finds the `Go_` block by the shape of its tables, so another SMPS 68k ROM locates the same way.  `core/drivers/games.py` holds Rev 01's three data fixes
(Marble Zone PSG3, Credits PSG2, SndBC Teleport FM5), the ROM's counterpart of the disassembly's
`FixMusicAndSFXDataBugs`.

## Coordination flags

A track byte below `$80` is a duration, `$80` a rest, `$81`-`$DF` a note (on the DAC track, a sample),
`$E0` and up a [coordination flag](../glossary.md#coordination-flag): a command with operands
(`CoordFlag` → `coordflagLookup`).

| Byte | Macro (alias) | Operands | What the driver does |
|------|---------------|----------|----------------------|
| `$E0` | `smpsPan` | direction + AMS/FMS | Writes `$B4`: speakers and LFO sensitivity.  Ignored on PSG |
| `$E1` | `smpsDetune` (`smpsAlterNote`) | signed | Sets `Detune`, added to every frequency write ([Pitch](#transposition-vs-detune)) |
| `$E2` | `smpsNop` | byte | Stores a byte the game can read; no sound |
| `$E3` | `smpsReturn` | — | Returns from `smpsCall` |
| `$E4` | `smpsFade` | — | Restores the music the 1-Up interrupted and fades it in; the 1-Up's track ends |
| `$E5` | `smpsChanTempoDiv` | byte | This track's tempo divider |
| `$E6` | `smpsAlterVol` | signed | Adds to the track's attenuation and rewrites the carrier TLs (`SendVoiceTL`) |
| `$E7` | `smpsNoAttack` | — | Holds the next note ([smpsNoAttack](#smpsnoattack-e7)) |
| `$E8` | `smpsNoteFill` | frames | Key-off timeout for every note from here ([Note fill](#note-fill-smpsnotefill)) |
| `$E9` | `smpsChangeTransposition` (`smpsAlterPitch`) | signed | Adds semitones to `Transpose` |
| `$EA` | `smpsSetTempoMod` | byte | New tempo modifier, its counter restarted ([Timing](#timing)) |
| `$EB` | `smpsSetTempoDiv` | byte | Every music track's tempo divider |
| `$EC` | `smpsPSGAlterVol` | signed | Adds to a PSG track's attenuation; heard from the next volume write |
| `$ED` | `smpsClearPush` | — | Lets the push-block SFX play again (SndA7) |
| `$EE` | `smpsStopSpecial` | — | Ends the track and hands FM4 back to the music (SndD0 Waterfall) |
| `$EF` | `smpsSetvoice` (`smpsFMvoice`) | voice | Loads an FM voice (`SetVoice`) |
| `$F0` | `smpsModSet` | wait, speed, change, step | Sets and enables modulation ([Modulation](#modulation-smpsmodset)) |
| `$F1` | `smpsModOn` | — | Enables modulation with the stored parameters (no Sonic 1 song uses it) |
| `$F2` | `smpsStop` | — | Ends the track: FM key-off, PSG silenced |
| `$F3` | `smpsPSGform` | byte | Turns the track into the noise channel ([Noise](#noise)) |
| `$F4` | `smpsModOff` | — | Disables modulation |
| `$F5` | `smpsPSGvoice` | `fTone_xx` | PSG volume envelope ([PSG envelopes](#psg-volume-envelopes)) |
| `$F6` | `smpsJump` | offset | Jumps |
| `$F7` | `smpsLoop` | slot, count, offset | Jumps back until the body has played `count` times |
| `$F8` | `smpsCall` | offset | Pushes the return address and jumps |
| `$F9` | `smpsMaxRelRate` (`smpsWeirdD1LRR`) | — | Writes `$0F` to FM1's `$88` and `$8C` (D1L 0, RR 15 on two operators; Spring Yard) |

The ROM reader drops `$ED` and `$F9` (no effect on the music it converts) and reads `$E4` and `$EE`
as the track's end.

## Pitch

### Note tables

**FM.**  `FMSetFreq` takes `byte − $80 + Transpose`, masked to 7 bits, into `FMFrequencies`: eight
octaves of twelve words, each `block << 11 | fnum` (the `$A4`/`$A0` register pair).  A row runs B to
A♯ (fnum 606 … 1148) because index 0 is the rest, so `nC0` is index 1.  An FM label is the real pitch
at transposition 0:

```
nA4 = fnum 1084, block 4:   f = fnum × 53267 × 2^block / 2^21 = 1084 × 53267 × 16 / 2097152 = 440.5 Hz
```

(Exactly 440 Hz would be fnum 1083: the table rounds.)

**PSG.**  `PSGSetFreq` takes `byte − $81 + Transpose`, masked to 7 bits, into `PSGFrequencies`: 70
SN76489 dividers.  Index *i* sounds C3 + *i* semitones (index 0 = divider 854 = 131 Hz), so a label
sounds three octaves above its name at transposition 0, one below with the usual header pitch `$D0`.
The rows are transcribed Hz values, not exact octaves (row 2 starts at 522.71 Hz, not 4 × 130.98).

Index 69 is `nMaxPSG` (= `nA5`, byte `$C6`): divider 0 in the ROM, which the chip plays as 1 — about
112 kHz, inaudible.  It is the noise channel's trigger note.  The transcription in
`core/drivers/reference.py` holds it as 1 (the asm macro's rounding of 223721.56 Hz).

The 7-bit mask **wraps, it does not clamp**: an index past the 70-entry table reads the code that
follows it.  Indices 125-127 (a note one to three semitones below the table, as Spring Yard PSG1 and
Credits PSG1 play) were measured from those songs' rips as dividers 0, 922 and 540.  Indices 70-124,
which no music reaches, continue the twelve-tone sequence instead of the code bytes
(`PSG_FREQUENCIES_EXTENDED`).

### Transposition vs detune

They are unrelated.

- **Transposition** (`$E9`, and the header's pitch byte as its starting value) is whole semitones,
  cumulative: it moves the table index of every later note.
- **Detune** (`$E1`) is a signed byte added to the frequency word after the lookup, on every write
  (`FMUpdateFreq`, `PSGUpdateFreq`), until changed.  On FM it is fnum units: one unit is 1.5 cents at
  A♯ (fnum 1148) to 2.9 cents at B (606), so `$03` is +4.5 … 8.5 cents — never a semitone, and the
  note index never sees it.  On PSG it adds to the divider, so a positive detune **lowers** the pitch.

How the converter plays either: [../conversion/notes.md](../conversion/notes.md),
[../conversion/detune.md](../conversion/detune.md).

## Timing

### Header

`smpsHeaderTempo divider, modifier`: the [tempo divider](../glossary.md#tempo-divider) goes to every
track, the [tempo modifier](../glossary.md#tempo-modifier) (*m*) is the main tempo.  A duration byte is multiplied by the track's divider as it is read (`SetDuration`, a byte:
the product wraps at 256).  `smpsChanTempoDiv` changes one track's divider, `smpsSetTempoDiv` every
track's; the last write wins.

### TempoWait

Each V-int (one video frame) the main tempo counter counts down from *m*; when it runs out, `TempoWait`
adds 1 to every music track's `DurationTimeout` and reloads the counter.  So every *m*-th frame no track
advances a [tick](../glossary.md#tick), which slows the song without changing any duration byte:

```
ticks per second = fps × (m − 1) / m        m = 3: 40 (NTSC), 33.3 (PAL);  m = 5: 48 / 40
```

Ticks are unevenly spaced: tick *k* of a segment is read on frame `k + k // (m − 1)`
(`core/smps/tempo.py`).  `smpsSetTempoMod` writes the tempo and its counter, so the holds start again
from where it is read.

Only `DurationTimeout` is held: note fill, modulation and PSG envelopes count every frame.  SFX tracks
are never held — one tick a frame.  The BPM this gives a MOD: [../conversion/timing.md](../conversion/timing.md).

## Note reads

When a track's `DurationTimeout` runs out the driver clears `smpsNoAttack`, reads flags up to a note
or duration, and then:

- **FM** (`FMUpdateTrack`): `FMNoteOff` on the byte read (note, rest or duration), the frequency
  written, `FMNoteOn` unless resting.  Every read re-keys the channel.
- **PSG** (`PSGUpdateTrack`): `PSGDoNoteOn` writes the divider and `PSGDoVolFX` the volume — the
  key-on, after a fill or rest silenced the channel.
- Each read restarts the fill, the PSG envelope and the modulation (`FinishTrackUpdate`), unless
  `smpsNoAttack` is set.

### A duration with no note

It takes the `.gotduration` path, skipping `FMSetFreq` / `PSGSetFreq`: it **re-keys** at the
frequency the track already holds — a transposition changed since is not applied (SndA8 SS Goal).
After a rest that frequency is cleared (FM 0, PSG −1), so the track keeps resting: Credits PSG3's
`nRst, $24` followed by 32 loops of bare `$03, $03, $06` is silent in the rip.  On the DAC track it
re-hits the saved sample; after a rest, nothing.

### smpsNoAttack ($E7)

It skips the next note's key-off, not its key-on.  `FMNoteOn` still writes the key-on, which an
already keyed channel ignores: after a rest, or once `smpsNoteFill` keyed the note off, the note
attacks.  The fill, envelope and modulation carry on from the note before.

- **FM:** the flag also blocks a fill's key-off during the held note (`FMNoteOff` checks it).
- **PSG:** `SetPSGVolume` writes nothing while the flag is set and the fill has run out, so the held
  note stays silent.

## Modulation (smpsModSet)

```asm
smpsModSet wait, speed, change, step     ; $F0 wait speed change step
```

| Operand | Field | Meaning |
|---------|-------|---------|
| `wait` | `ModulationWait` | Frames before modulation starts |
| `speed` | `ModulationSpeed` | Frames per step, reloaded from the data each step |
| `change` | `ModulationDelta` | Signed; added per step to the frequency word: fnum on FM, divider on PSG |
| `step` | `ModulationSteps` | Steps per half-swing — **halved for the first half-swing only** |

`smpsModSet` and every attack store `step / 2` (`lsr.b #1`), but when the counter runs out
`DoModulation` reloads it from the **original** byte, negates the delta and spends that update (a
pause at each turn).  With `step` `$04`: 2 steps up, then 4 down, 4 up, … — a triangle of
`delta × step / 2` either side of the note with a steady cycle of `2 × speed × (step + 1)` frames.

Example: `$00, $01, $06, $04` gives 10 frames a cycle = 6 Hz, ±12 fnum; the Title Screen rip measures
5.99 Hz.  An odd `step` leaves the triangle half a delta off-centre.  No operand is multiplied by the
tempo divider.  The `4xy` it becomes: [../conversion/effects.md](../conversion/effects.md).

## Note fill (smpsNoteFill)

[Note fill](../glossary.md#note-fill) (`smpsNoteFill n`) keys every note off `n` **frames** after it attacks (FM key-off, PSG silence); 0
turns it off.  It persists until changed: each attack reloads `NoteTimeout` from the stored value.
The duration still decides when the next note starts.

- The fill counts frames, the duration ticks: with `smpsHeaderTempo $01,$05` a duration of 12 lasts
  250 ms, a fill of 12 lasts 200 ms (the Title Screen rip agrees).
- A fill equal to the duration fires when the modifier is > 1 (the note lasts `duration × m / (m − 1)`
  frames).  Only with no hold frame in the note does the next read win the tie.
- The fill is never multiplied by the tempo divider.

How it becomes a note cut: [../conversion/effects.md](../conversion/effects.md).

## FM voices

### Voice layout

25 bytes: `feedback << 3 | algorithm`, then six groups of four — DT/MUL, RS/AR, AM/D1R, D2R, D1L/RR,
TL — each group in SMPS operator order 4, 3, 2, 1 (the `smpsVc*` operands reversed).

### Operator order

`SetVoice` writes each group to operator offsets `$00, $08, $04, $0C` (`FMInstrumentOperatorTable`),
so SMPS operator *n* is the chip's operator 5 − *n*:

```python
# core/smps/driver_tables.py: read by core/synth/fm_render.py, core/rom/voices.py and sfx/chips.py
SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)   # SMPS OP1..OP4 → chip OP4, OP3, OP2, OP1
```

> **Pitfall:** the order `(0x00, 0x08, 0x04, 0x0C)` puts SMPS operator 1 — chip OP4, a carrier in
> every algorithm — into OP1's feedback slot: an "overdriven guitar" distortion.

### Carriers by algorithm

| Algorithm | Carrier offsets | Chip operators |
|-----------|-----------------|----------------|
| 0–3 | `$0C` | OP4 |
| 4 | `$08`, `$0C` | OP2, OP4 |
| 5, 6 | `$04`, `$08`, `$0C` | OP2, OP3, OP4 |
| 7 | all | OP1–OP4 |

`FMSlotMask` names them.  `SetVoice` adds the track's attenuation (header volume + `smpsAlterVol`)
to the carrier TLs with `add.b` — modulo 256, the chip reading 7 bits.  `SendVoiceTL`, after
`smpsAlterVol`, rewrites the carriers only, skips one whose sum carries past `$FF`, and does nothing
while the attenuation is negative.  Several carriers at TL 0 overflow the chip's 9-bit channel
accumulator; that clipping is the hardware's ([../synthesis/fm.md](../synthesis/fm.md)).

## DAC

The Z80 plays DPCM samples through the YM2612's DAC (FM6).  The DAC track writes each sample byte to
`zDAC_Sample`; it has no key-off, fill or modulation, and a rest does not stop a sample: it plays out.

### Sample table

| Constant | Byte | Sample | Driver pitch |
|----------|------|--------|--------------|
| `dKick` | `$81` | kick | 23 |
| `dSnare` | `$82` | snare | 1 |
| `dTimpani` | `$83` | timpani | 27 |
| `dHiTimpani` | `$88` | timpani | 18 |
| `dMidTimpani` | `$89` | timpani | 21 |
| `dLowTimpani` | `$8A` | timpani | 28 |
| `dVLowTimpani` | `$8B` | timpani | 29 |

`$88`-`$8B` are the 68k's: it writes the pitch from `DAC_sample_rate` into the timpani's table entry
(`zTimpani_Pitch`) and plays `$83`.  The pitch stays, so a bare `$83` afterwards plays at the last one
(no Sonic 1 song plays `$83` itself).  `$84`-`$86` are not samples; from `$87` the Z80 plays the SEGA
voice.

### Playback rates

The pitch is a loop counter.  The disassembly computes each sample's from its source WAV's rate
(`dpcmLoopCounter`): kick 8,250 Hz, snare 24,000, timpani 7,375; the timpani variants scale that rate
by 1.30, 1.20, 0.97 and 0.95.

**What hardware plays.**  `zPlayPCMLoop` takes exactly `301 + 26 × (pitch − 1)` Z80 cycles a byte (two
samples) at 3,579,545 Hz, counted from the ROM's code (`core/drivers/smps68k/sonic1/dac.py`, the
format in `core/rom/pcm/smps.py`; `z80.asm` matches).  Wait states can only add to that.  On top, the
68k stops the Z80 for the whole music update once a frame (`UpdateMusic`'s `stopZ80`): the DAC holds
for 3.8–6.4 % of the time, by song.  The hardware rate is the cycle count less that share.

| Sample | Pitch | Cycle count | Hardware at a 5 % stall | MOD note |
|--------|-------|-------------|-------------------------|----------|
| dKick | 23 | 8,201 Hz | ~7,790 Hz | B1 |
| dSnare | 1 | 23,784 Hz | ~22,590 Hz | F3 |
| dTimpani | 27 | 7,328 Hz | ~6,960 Hz | A1 |
| dHiTimpani | 18 | 9,635 Hz | ~9,150 Hz | D2 |
| dMidTimpani | 21 | 8,720 Hz | ~8,280 Hz | C2 |
| dLowTimpani | 28 | 7,138 Hz | ~6,780 Hz | A1 |
| dVLowTimpani | 29 | 6,957 Hz | ~6,610 Hz | G#1 |

Worked example: the kick, `3,579,545 / (301 + 26 × 22) × 2 = 8,201 Hz`.

> **Note:** the VGZ rips are not the yardstick for DAC rates.  Their emulator runs the loop 1.7–2.8 %
> fast between the stalls (snare 24,440 Hz against the count's 23,784), so their averaged rates land
> 2–3 % above the hardware's, and `vgm_compare` reads the DAC 1–3 % flat against a rip.

Choosing a config's note and finetune from these: [../guide/song_configs.md](../guide/song_configs.md).

## PSG

Three SN76489 tone channels (PSG1–PSG3) and a noise channel, which PSG3's track drives once it is a
noise track.  Pitch: [Note tables](#note-tables).  The synthesis: [../synthesis/psg.md](../synthesis/psg.md).

### PSG volume envelopes

`smpsPSGvoice fTone_0n` (or the header's voice) selects envelope `PSGn` of nine; 0 is none.  From each
attack one step a frame is added to the track's attenuation (header volume + `smpsPSGAlterVol`),
capped at `$F` (silence).  The `$80` terminator holds: `VolEnvHold` steps the index back and writes
nothing, so the last value stays.  An envelope shapes the amplitude only.

Example, `fTone_02` (`PSG2`): `0, 2, 4, 6, 8, $10, $80` — 2 attenuation steps (4 dB) quieter each
frame, silent from the sixth.

### Noise

`smpsPSGform` (`cfSetPSGNoise`) sets the track's `VoiceControl` to `$E0` for good — no flag turns it
back — and writes its byte to the noise register: `$E0 | white << 2 | rate`.  Rates 0–2 are fixed
clocks; rate 3 clocks the LFSR from PSG3's tone divider (the chip's tone channel 2), which the track's
notes go on writing.  Every
Sonic 1 noise track uses `$E7` (white, rate 3): `nMaxPSG` gives the fastest hiss, other notes pitch the
noise.  `smpsPSGvoice` afterwards changes only the envelope.

## How it converts

Sonic 1 is the reference the converter's rules are written in: [../conversion/overview.md](../conversion/overview.md).
Its songs convert from the asm (full configs in `configs/sonic_1/`) or from the ROM (`rom_song:`, or
`--input` / `--rom-song` on the command line):

```bash
python convert.py configs/sonic_1/02_green_hill_zone.yaml --input input/roms/sonic_rev01.bin --rom-song '$81'
python tools/rom/rom_import.py input/roms/sonic_rev01.bin --compare reference/smps_drivers/sonic_1
```

The ROM and the asm read to the same ops; `rom_import.py --compare` checks every sound.

### Known limits

- Data errors in the songs are converted as meant, not as shipped (data fixes on by default; the
  ROM's `--shipped`, the parser's `fix_data_bugs=False` read the game as sold).
- PSG table indices 70-124 are a plausible continuation, not the code bytes the driver reads there.
- `smpsClearPush` and `smpsMaxRelRate` are not converted.
