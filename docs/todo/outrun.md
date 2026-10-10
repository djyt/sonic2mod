# OutRun: an SMPS Z80 on Timer A

Probed 2026-10-10.  Goal: `convert.py` reads OutRun's songs from the ROM, as it reads Sonic 1,
Moonwalker, Golden Axe, Streets of Rage and Space Harrier II (`docs/smps_variants.md`).

Status key: `[ ]` open, `[x]` done.  Four parts: what the driver is (1), what the code must change
(2), the timing design, the one new concept (3), the plan (4).

**The ROM is the source of truth.**  Every value (rates, timing, pitch) is read or counted from
the driver's code.  The rips corroborate it.  Where one disagrees, the code is read again: a rip is
only as good as its emulator and its ripper (Sonic 1's play DAC 2-3 % fast; some lose frames).

---

## 1. Analysis (probe 2026-10-10)

`GM 00004052-00` (USA, Europe), SHA-1 `95d2055f…`.  No disassembly: read by hand from the Z80 driver's
bytes (no Z80 disassembler was used: check the details with z80dis before building on them).  5 rips
(the full soundtrack): `reference/vgz/outrun/`.  The rip's notes credit Hertz as the developer.
Moves to `docs/smps_variants.md` when done.

**Detection today.**  All five drivers reject it: `sonic1`, `type1a` (no 68k FM octave), `mucom`,
`type0fm` and `sh2` (no Z80 program load found).

**Family.**  Golden Axe's layout (sound header at the bank's start, word pointers, `$FB`
transposition, `$FC` / `$FD` / `$FE` as Type 0 FM, a handler then `inc de` dispatch), with a
Sonic-style flag set, Sonic 1's voice layout and DPCM, a DAC track, and a clock of its own.

**Where things are**
- **Program:** ROM `$F60A8`, `$13B2` bytes, to Z80 `$0000`.  The 68k (`$65D4`) copies it through
  pointers: `movea.l ($EFFF8).l,a0 / move.l ($EFFFC).l,d0 / subq.l #1,d0 / lea $A00000,a1 /
  move.b (a0)+,(a1)+ / dbra d0`.  `core/rom/z80.py` knows no such load.  Before releasing the Z80
  the 68k writes `$A01C00`-`$A01C07`: `0F 00 00 80 5A 02 0F 80` (the two banks, the header base
  `$8000`, Timer A `$025A`).
- **Banks:** set by 9 writes to `$6000` from a RAM word: `$1C00`/`$1C01` -> ROM `$F0000` (songs,
  the driver's own bytes in it), `$1C06`/`$1C07` -> ROM `$F8000` (DAC samples).
- **Sound header (bank `$F0000`):** `+0 $8451`, `+2 $1016`, `+4` music index `$8479`, `+6` SFX index
  `$1000` (Z80 RAM: the SFX are in the driver program), `+8 $800E` (envelopes?), `+A $80C0`.
- **Driver tables:** PSG `$0A13` (Sonic 1's dividers, `$356` down, ended by 0), FM `$0AA1` (one
  octave), flag jumps `$0B25` (`$E0`-`$FF`), `$FF` sub-command jumps `$0B65` (8), voice register order
  `$05E8`, DPCM deltas `$0063`.
- **RAM:** track RAM `$1C40`, `$30` a track; the sound dispatch is at `$0631` (`$81`-`$8A` music
  loader, `$8B`-`$8D` commands, `$8E`-`$9A` and `$9B`-`$A8` SFX).

**Songs.**  5, `$81`-`$85` (the index runs into data after them).  A standard SMPS header: voices,
FM count, PSG count, divider, tempo, then 4-byte FM and 6-byte PSG entries.  The FM entries take
channels from the init table `$0706`: DAC (6), FM1 FM2 FM3 FM4 FM5 FM6.  FM3 is a normal channel.

| song | FM | PSG | divider | tempo | header |
|------|----|-----|---------|-------|--------|
| `$81` | 6 | 3 | 1 | 0 | `$87F5` |
| `$82` | 6 | 3 | 1 | 0 | `$9FC9` |
| `$83` | 6 | 3 | 1 | 6 | `$B8CA` |
| `$84` | 7 | 0 | 2 | 5 | `$C9AF` (DAC track has no notes) |
| `$85` | 6 | 3 | 1 | 0 | `$CECA` |

Which rip is which song: not yet matched.

**How it plays**
- **Clock: YM2612 Timer A, not V-int.**  The main loop polls the YM status (bit 0) and runs the
  update on each overflow; Timer A is `$25A`: 144 x (1024 - 602) / 7 670 453 = 7.92 ms, **~126.2 Hz**.
  The rips confirm it: every song rewrites `$24 = $96`, `$25 = $02`, ~2 writes an update.  `$EA` sets
  Timer A (2 bytes), `$EB` adds to it; no song uses either.
- **Tempo:** as TempoWait: every `tempo` updates each track's duration counter is held one (`$09B0`);
  0 never holds.  The phase (where the hold falls against the track update) is not read yet.
- **Pitch:** note - `$81`, 12 a block (`$02F9`); FM fnum from the one-octave table `$284`…`$4C0`.
  The pitch is the table's word: `$81` plays fnum `$284` at block 0 (C♯ in Sonic 1's table).  Only
  the label is open: name notes by the frequency they play.
  PSG from `$0A13` (69 dividers).
- **Voices:** 25 bytes, Sonic 1's order: `$B0`, DT/MUL, KS/AR, AM/D1R, D2R, D1L/RR, TL (`$05E8`:
  `30 38 34 3C 50 …`).  `$EF` with bit 7 set takes a voice from another song's table (second
  operand: the song); no song does.
- **DAC:** streamed in the main loop between Timer A polls.  13 samples (`$81`-`$8D`) in bank
  `$F8000`, 6 bytes each (the table at the bank's `$8000`, read by `ld hl,$8000` at `$01E2`, no
  `ld iy`): pointer, length, a delay-loop count (the rate), a flags byte: bit 0 raw 8-bit PCM
  (`$89`, `$8B`-`$8D`), clear Sonic 1's DPCM (the same delta table); its high nibble is written to
  `$B6` (FM6's pan, AMS / FMS) as each sample starts (`$0194`): the pan is the sample's.
  The rate is a `djnz` loop inside the polling loop: count its Z80 cycles, the time the updates
  take between outputs included (as Sonic 1's, `docs/smps_driver.md`).
- **Flags** (operands: what the handler reads; the dispatch steps one past it):

  | byte | OutRun | ops | music uses |
  |------|--------|-----|-----------|
  | `$E0` | pan / AMS / FMS (`$B4`) | 1 | 120 |
  | `$E1` | detune | 1 | 16 |
  | `$E2` | communication byte (`$1C16`) | 1 | - |
  | `$E3` | stop (calls `$09DF`, then `$F2`'s) | - | - |
  | `$E4` | 5 bytes to the track (`+$11`), `+$16` = 1 | 5 | - |
  | `$E5` | set FM volume | 1 | - |
  | `$E6` | alter FM volume | 1 | 355 |
  | `$E7` | no attack | 0 | 760 |
  | `$E8` | note fill | 1 | - |
  | `$E9` | LFO (`$22`) | 1 | - |
  | `$EA` `$EB` | Timer A set / add | 2 | - |
  | `$EC` | alter PSG volume | 1 | 2 |
  | `$ED` `$EE` | YM register write, part I / II | 2 | - |
  | `$EF` | voice (2 operands when bit 7 is set) | 1 | 175 |
  | `$F0` | modulation | 4 | - |
  | `$F1` | modulation on (sets `+$07`) | 2 | - |
  | `$F2` | stop | - | 22 |
  | `$F3` | PSG noise | 1 | 7 |
  | `$F4` | `+$07` = operand (modulation off?) | 1 | 2 |
  | `$F5` | PSG voice | 1 | 129 |
  | `$F6` `$F7` `$F8` `$F9` | jump, loop, call, return | 2, 4, 2, 0 | |
  | `$FA` | divider | 1 | - |
  | `$FB` | transposition (add) | 1 | 90 |
  | `$FC` `$FD` | slide mode, raw frequency mode (as Type 0 FM) | 1 | - |
  | `$FE` | FM3 special mode | 4 | - |
  | `$FF xx` | 8 sub-commands: `00` none, `01` tempo add, `02` play a sound, `03`, `04` copy n bytes, `05`, `06`, `07` (2) | 1+ | 1 (`FF02`, `$84`) |

  A scratch walk with these lengths reads every track of all 5 songs to its end.

---

## 2. What the code must change

### 2.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. [ ] **The update rate from the rules** (§3.3): a frame's length comes from the song's driver,
   not the config's region alone.
2. [ ] **The pointer load** in `core/rom/z80.py`: a copy to `$A00000` whose source and length are
   longs the 68k reads from ROM.
3. [ ] **A one-octave FM table** in `smpsz80/program.py`: `_find_fm_table` wants a second octave a
   block up; build the note table from 12 words and a computed block.
4. [ ] **`PcmTable` beyond Z80 RAM** (`core/drivers/smps68k/dpcm.py`): it reads the table and the
   samples from Z80 RAM, finds the table by `ld iy,nn` and plays every sample in one format.
   OutRun's are in a ROM bank, the table at its start, and each entry's flags choose raw PCM or
   DPCM.  Take the memory and the table's address as arguments, a format per entry; a raw PCM
   `SampleFormat`.  A Z80 driver using it: the module moves out of `smps68k/` to a shared place.

### 2.2 New concepts

| Fact | IR | Resolved in | Downstream |
|---|---|---|---|
| Timer A clock | the update rate (§3) | rules | a frame's length in seconds: BPM, renders, DAC rates, rip frames |
| a sample's own pan | a `PAN` event (existing) at each DAC note, from the sample's flags | walk | none |

The flags are no new concept: each maps to an existing `CoordFlag` (Phase 1.3).  Nor is the DAC
track: `SmpsVariant.dac`, `DacSample`, `PcmTable` / `PcmEntry` (a 6-byte entry: size at +2, pitch
at +4), `Dpcm` and rates counted from the play loop, as Sonic 1's, Moonwalker's and Streets of Rage's
`dac.py`.  What differs is 2.1.4.

---

## 3. Timing: the design (agreed 2026-10-10)

### 3.1 A frame is one run of the driver's update

Everything the song model counts is counted in **frames**: TempoWait's holds (`core/smps/tempo.py`),
note fill, modulation's wait and speed, PSG and pitch envelopes, `key_run_out`, drum programs'
steps, `EDx` delays.  On every driver so far a frame is a V-int.  On OutRun's it is a Timer A
overflow.  Either way the driver does the same thing each frame, so **the word keeps its meaning**:
one run of the update, whatever triggers it.  (Renaming it "update" through the code is churn for
no behaviour.)

What follows from that: **nothing counted in frames changes.**  The walk, the tempo schedule,
`Timeline.ticks_per_frame_at`, vibrato (`core/convert/vibrato.py`: frames x ticks a frame, "both
clocks scale with the frame rate") are all rate-free.  Only **a frame's length in seconds** is new.
It enters at the edges: BPM, seconds, renders that step a frame at a time, DAC rates, and the rip
tools' frame logs.

### 3.2 The rate is the driver's

- **`PlaybackRules.update_clock`**: `VINT` (the default: every existing driver) or
  `TimerA(value)`.  It is a fact of the driver, not of the song config.  OutRun's value is read from
  the ROM (the 68k's init write to `$A01C04`, `$025A`), not stated.
- **One function, `update_hz(rules, region) -> float`**:
  - V-int: 60 / 50, the converter's convention today (`REGION_FPS`; 60, not 59.92).
  - Timer A: the FM clock / (144 x (1024 - value)).  The period law is a chip fact:
    `core/chips/fm.py` (`timer_a_hz(value, clock)`, beside `MD_FM_CLOCK`).  NTSC: 7 670 454 /
    (144 x 422) = **126.22 Hz**.  A PAL config would need the PAL FM clock (7 600 489, 125.07 Hz).
    No config is PAL today.
- **Rate changes mid-song** (`$EA` set, `$EB` add): refused, so a song using one fails loudly.
  None does.  Supporting them later means rate segments, as tempo segments are now; not designed
  here.

### 3.3 Where it is read (refactor 2.1.1, byte-identical)

The frame rate is the config's `region` today, read in these places.  Each one reads the driver's
rate instead:

| Site | Today | Reads |
|---|---|---|
| `ConversionConfig.fps` (`core/config/song.py:249`) | `region_fps(region)` | removed: it has no song |
| `convert.py:159` (auto BPM) | `config.fps` | `update_hz(song.rules, region)` |
| `core/convert/smps2mod.py:641` (noise sample lengths) | `config.fps` | the timeline's |
| `core/convert/channel_writer/cells.py:90` (cut ticks) | `config.fps` | the timeline's |
| `core/convert/smps2mod.py:561` (FM drums) | `region_fps(config.region)` | the timeline's |
| `core/synth/fm_samples.py:140` (stepped renders: `frame_hz`) | `region_fps(config.region)` | passed in |
| `core/synth/psg_samples.py:320` (envelope steps) | its own `50.0 if pal else 60.0` | passed in |
| `core/merge/notes.py:100` | `region_fps(config.region)` | passed in |
| `core/plan/derive.py:285` (minimal configs' BPM options) | `region_fps(stated region)` | `update_hz(song.rules, region)` |
| `core/analysis.py:210, 213` (`analyze.py`: both regions' BPM) | 60, 50 | each region's `update_hz` |

- **Resolved once a conversion:** `Timeline(song, config)` has both, so it holds `fps`.  Sites with
  a timeline read it there.  Renderers take the number as they already take `frame_hz` / `fps`.
  `region_fps` survives only inside `update_hz`.
- **int becomes float** (V-int: 60.0): the gates prove every MOD byte-identical
  (`regression.py --all`, `tool_regression.py --all`).
- **Render cache:** PSG renders key on `fps`, stepped FM renders on `frame_hz`, so a Timer A song's
  renders key apart unchanged.  Plain FM renders do not step frames: no key change.

### 3.4 What the MOD resolves

- A MOD tick is BPM x 2 / 5 Hz: at most 102 Hz (`Fxx` 255), coarser than a 126.22 Hz frame.  Per-frame events (note fill cuts, `EDx`, vibrato steps) round to MOD ticks, as they
  already do on V-int songs played below 60 ticks a second.  No new rule.
- BPM: `derive_bpm` and derive's options already take the rate.  Tempo 0, divider 1 is 126.22
  ticks a second.  For example, ticks_per_row 6 at speed 4 is BPM 210.4 -> 210 (0.2 % slow).  Each
  song's grid is checked in Phase 2.

### 3.5 DAC rates: the update runs inside the sample loop

The play loop polls Timer A between outputs (`ld a,($4000) / bit 0,a / call nz,$0038`).  While the
update runs, no byte is output.  So a sample's rate is two counts from the code:
- **the loop:** Z80 clock / cycles an output (`dpcm.py`'s model), exact;
- **the stall:** the update's cycles x 126.22 a second.  This stands where the 68k's bus hold does on
  Sonic 1 and Streets of Rage (`mucom/dac.py` `_STALL`), but it is far larger.  Passing Breeze's rip
  pauses the DAC a mean 138 VGM samples (3.1 ms, ~11 000 Z80 cycles) each 350-sample frame: **~40 %
  of the time.**  The loop's rate alone would play the drums ~1.6x fast.

The update's cost varies with what it does each frame (tracks keyed, busy waits).  How to count it
is decision T5.

### 3.6 Rip frames: cut at the Timer A reset

`core/vgm/frames.py` cuts a log into fixed V-int windows (`VGM_SAMPLE_RATE // header.rate`: 735)
at the commonest burst phase.  A Timer A rip's frame is 349.4 samples, not a whole number.  But
the rip marks every frame itself: each update reloads the timer (`$25`, `$24`, then `$27 = $1F`,
called from `$01A8` early in the update).  Its register writes follow 225-325 samples later, before
the next reset.  So:
- **a frame log for a Timer A driver is cut at each `$27` reset write** (frame n = from reset n to
  reset n + 1), not at a fixed period.  `FrameLog.frame_of` becomes a lookup in the frame starts;
  V-int logs keep the fixed cut.  The tools that read frames (`vgm_frames`, `vgm_lift`, glitches,
  realign) work unchanged on frames.  Choosing the cut needs the driver (the rips' tools know it
  from the config's song).
- **The rips run slow.**  Passing Breeze: a mean 350.39 samples a frame against the chip's 349.38
  (0.29 %, ~0.9 s over the song).  Likely the ripping emulator's (a timer stepped per scan line
  would round 124.6 lines up to 125: 350.6), not the hardware's.  The chip's own behaviour on these
  writes is read from Nuked-OPN2 (`3rdparty/`).  The converter plays the chip's rate.
  Frame-based tools are unaffected.  Time-based audits (`vgm_compare`) must allow for the rip's
  rate: decision T6.

### 3.7 Decisions (the user's, 2026-10-10)

- **T1 Name:** "frame" stays, documented as one run of the driver's update.  Not renamed to
  "update" through the code.
- **T2 Home:** the rate on `PlaybackRules` (`update_clock`), read from the ROM by the driver; the
  config's `region` only picks the clock.  Not a config key.
- **T3 One function, refactor first:** `update_hz(rules, region)`; the sites of 3.3 through it,
  `ConversionConfig.fps` removed; Phase 0, every MOD byte-identical before the driver work starts.
- **T4 `$EA` / `$EB`:** refused until a song needs them; no rate segments now.
- **T5 The DAC stall:** counted from the code: the update's common path, an estimate (its cost
  varies a frame); the rips' DAC pauses a check.  Not a Z80 core, not left out.
- **T6 Audits against slow rips:** `vgm_compare` measures the rip's frame length from its reset
  writes and scales the reference by it before aligning.  The rip's rate never enters a conversion.

---

## 4. Plan

### Phase 0: refactors
- [ ] 0.1 2.1 items 1-4 (item 1: §3.3, T1-T3); gates (regression, tool regression, pytest, ruff, pyright, vulture, layers).

### Phase 1: read the ROM
- [ ] 1.1 `SmpsDriver.OUTRUN` (`smpsz80_outrun`), `core/drivers/smpsz80/outrun/`, pinned by SHA-1
  in `games.py` (D1).
- [ ] 1.2 Locate by shape: the pointer-loaded program, the bank words in `$1C00` / `$1C06`, the
  sound header's music index.  SFX: in Z80 RAM (`$1000`).
- [ ] 1.3 Flags (the table in 1) as existing `CoordFlag`s; `$FF xx` (operands by sub-command) and
  `$EF` (a second operand when bit 7 is set) as `FlagKind.READ`; `FF02` dropped (each song alone).
  Sonic 1's voice layout, header (DAC first).  Check each handler's operand count with a
  disassembler.
- [ ] 1.4 `rom_import.py`, `song_dump.py`; read snapshot `read_outrun`; unit tests.

### Phase 2: timing and pitch
- [ ] 2.1 `update_clock = TimerA(value)` from the ROM's init write (§3.2), `$EA` / `$EB` refused;
  the TempoWait phase read from the code, the rips of `$83` and `$84` (the songs with a tempo) a
  check.
- [ ] 2.2 Frame logs cut at the Timer A reset writes for a Timer A driver (§3.6); `vgm_frames` and
  `vgm_lift` on the 5 rips.
- [ ] 2.3 The note labels from the frequency each table word plays (`$81` = `$284`, block 0);
  `vgm_pitch_audit` a check.
- [ ] 2.4 Match the songs to the rips; `rips.yaml`.

### Phase 3: DAC
- [ ] 3.1 `dac.py` on the shared `PcmTable` (2.1.4): the 13 samples, DPCM and PCM; each rate from
  the cycles of its play loop (both poll Timer A between outputs), less the update's stall (§3.5,
  T5).  The rips' rates and pauses a check.
- [ ] 3.2 Each sample's pan as a `PAN` at its DAC notes.

### Phase 4: convert and verify
- [ ] 4.1 Minimal configs (`configs/outrun/`); every song converts; `vgm_lift`, `vgm_frames`,
  `vgm_pitch_audit`, `vgm_compare` (the rip's frame length allowed for, T6), `measure_volumes
  --configs configs/outrun`.  A difference is
  read against the code before either side is changed; a rip's own glitches go in `rips.yaml`.
- [ ] 4.2 Cases in `tests/cases.yaml` (chosen by coverage).
- [ ] 4.3 `docs/smps_variants.md` § OutRun (section 1 moves there); CLAUDE.md index.

### Decisions (the user's, 2026-10-10; timing: T1-T6 in §3.7)
- **D1 The driver's name:** `smpsz80_outrun`, after the game, as Space Harrier II's `smpsz80_sh2`:
  no source found classes the Mega Drive port's driver.  Renamed for its SMPS class if one is found
  (ValleyBell's SMPS research, SMPSPlay), or for the family once a second game reads with it.

### Decisions (open)
- SFX (in Z80 RAM): music only, as Space Harrier II (D4 there)?
