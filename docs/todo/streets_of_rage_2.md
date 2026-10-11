# Streets of Rage 2: Mucom-MD, MUCOM88 songs on the 68k

Planned 2026-10-10.  Goal: `convert.py` reads Streets of Rage 2's songs from the ROM, as it reads
Streets of Rage (`docs/todo/done/streets_of_rage.md`, `docs/smps_variants.md` § Streets of Rage).
Nothing implemented yet; this is the probe and the plan.

Status key: `[ ]` open, `[x]` done.  Four parts: what the driver is (1), how close it is to
Streets of Rage's (2), what the code must change (3), the plan (4).  Open decisions for the user: § 5.

---

## 1. Analysis (probe 2026-10-10)

`Streets of Rage 2 (USA).md`, `GM MK-1054 -00`, 2 MB, SHA-1 `8b656eec9692d88bbbb84787142aa732b44ce0be`
(from `C:/emu/megadrive/sega-genesis-romset-ultra-usa/`; not yet in `input/roms/`).  Read with capstone
(68k) and z80dis (Z80) in scratch; every song walked by a scratch reader of the grammar below.  No
rips yet: nothing below is checked against a VGZ.

**It is not SMPS.**  `$40010`: `"  MUSIC DRIVER  Mucom-MD Ver0.85Programmed by T.Maruyama(M.N.M Software)"`.
A 68k port of MUCOM88 (the PC-88 MML driver): its songs are MUCOM88's compiled images, byte for
byte (header `00 C2` = the PC-88's load address `$C200`), on an engine of its own.  Streets of Rage
1's driver is SMPS 68k's engine reading the same track language (§ 2).

### 1.1 Where things are

| What | ROM | Note |
|---|---|---|
| Entry points | `$40000` | `bra`s: `$40000` music update, `$40004` SFX update (both from V-int, `$794`; PAL: again when `$FCFF` reaches 5), `$40008` a command (d0 word), `$4000C` init |
| Commands | `$40192` | `$00nn`: 0 stop, 1-6 fades / pause, `$E0`+ a channel's mute; `$01nn` song nn (`$40488`); `$02nn` a voice clip (Z80 sound `$8A + nn`); `$03`-`$06nn` SFX |
| Sound index | `$429CE` | longs relative to the table: `$00`-`$1B` music (28), `$1C`-`$1F` empty, `$20`-`$56` SFX (55) |
| Track RAM | `$FFA014` | `$A0` bytes each: FM1 FM2 FM3 (`$FFA014`/`0B4`/`154`, part I), PSG1-3 (`$FFA1F4`/`294`/`334`), FM4 FM5 (`$FFA3D4`/`474`, part II), the drum track (`$FFA514`, on FM6); SFX: FM `$FFA5B4`/`654`, PSG `$FFA6F4`/`794` |
| FM update | `$40E46` | note read `$40F06`; flags `$41B4C`, table `$41B74`; `$FF` prefix `$41B94`, table `$41BA8` |
| PSG update | `$42030` | flags `$423C4`, table `$423E8`; envelope `$422F8` |
| Drum update | `$4262E` | flags `$42836`, table `$4285A`; `$FF` prefix table `$4288E`; trigger `$427B2` |
| Voice load | `$416B0` | `$41646` first sets the four D1L/RR to `$FF`; voice = bank + 25 n |
| Volume -> TL | `$41A9A` | carrier masks `$41A88` (`01 01 01 01 03 07 07 0F`) |
| FM volume | `$4291A` | 20 TLs, `36 33 30 2D 2A 28 ... 05 02` |
| FM octave | `$4293E` | 12 words C-B, `$284`-`$4C0`: **Streets of Rage's table, byte-equal** |
| PSG octave | `$42956` | 12 words C-B, `$D5C`-`$714` (dividers × 8): a note's divider = word >> octave |
| PSG envelopes | `$4296E` | 16 presets × 6 bytes (MUCOM's SSG soft envelope: AL AR DR SL SR RR) |
| Z80 player | `$559C2` | 8 KB, copied raw to Z80 RAM (`$40D80`, no compression) |

### 1.2 Song header (MUCOM88's image, `$40488`)

```
+0  $C200.w  end.w (LE: the PC-88 load address and end; end - $C200 = the image's size)
+4  $00
+5  voices.w        LE, relative to +5: the voice bank (25-byte voices)
+7  ?.w             LE (not read by the 68k)
+9  $00             the base the track pointers are relative to
+10 11 entries, MUCOM88's channels A-K: ptr.w loop.w (LE, relative to +9; loop 0 = none)
    A B C = FM1-3, D E F = PSG1-3 (tone 1-3), G = rhythm (skipped), H I = FM4-5,
    J = FM6 (skipped: every song's is empty), K = ADPCM -> the drum track (FM6 as DAC)
```

No tempo, no per-track volume or transposition.  A track's `$00` jumps to its own loop pointer (the
header's), or stops it (`$41CA4`).  All 28 songs' 10 tracks walk to their `$00` without a desync;
song `$19` has no loop pointer at all (a one-shot), `$0C` and `$10` are empty (every track one `$00`).

### 1.3 Track code

`d n` a note (`d` `$01`-`$7F` frames, `n` octave << 4 | semitone; FM octave o = block o, C4 = o4),
`$80|d` a rest, `$00` the end, `$F0`-`$FF` a flag.  The driver runs once a V-int: a tick is a frame.

| Byte | FM | PSG | Drum track | Uses (bytes: FM / PSG / drums) |
|---|---|---|---|---|
| `$F0 n` | voice n | envelope preset n (`$4245C`) | sample `$80 + n` | 981 / 176 / 586 |
| `$F1 v` | volume: step = v - 2, clamped 0-19, TL = table[step] | volume 0-15 (att = v ^ 15) | (FM's) | 871 / 183 / 39 |
| `$F2 lo hi m` | detune word, m 0 sets, else adds | same; `>> octave` as it plays | skipped | 584 / 96 / 0 |
| `$F3 q` | gate: key off q frames before the end | same | (no effect) | 600 / 125 / 23 |
| `$F4 s ...` | vibrato: 0 set (delay speed depth.w count), 1 off, 2 on, 3-6 one field | same, depth × 266 / 256 | ignored | 588 / 284 / 0 |
| `$F5 o.w` / `$F6 x n back.w` / `$FE o.w` | loop start / end (n passes) / leave on the last pass | same | same | 1192 loops, 283 exits |
| `$F7 a b c d` / `$F7 n` | FM3 special mode, OP4 OP3 OP2 OP1 offsets | n ≠ 8: this track takes the noise channel; 8: gives it back | 4 bytes skipped | 44 / 179 / 0 |
| `$F8 n` | pan: `ror.b #2` -> 1 R, 2 L, 3 C, 0 none | noise rate: n 0-11 -> 0, 12-22 -> 1, else 2 | pan (FM6) | 928 / 4 / 0 |
| `$F9 x` | skipped | skipped | skipped | 0 |
| `$FA r v` / `$FA` × 6 | YM register write (up to 8 queued, `$41C74`) | envelope, 6 bytes direct | YM write, part I | 561 / 4 / 5 |
| `$FB n` | volume step += n, clamped 0-19 | volume += n, clamped 0-15 | (FM's) | 2177 / 981 / 4 |
| `$FC f p a` | LFO: `$22` = f \| 8, B4 FMS p AMS a | **no operands** (`rts`) | no operands | 22 / 0 / 0 |
| `$FD` | tie (read after the note: the note ties into the next) | same | same | 356 / 135 / 4 |
| `$FF s ...` | prefix: `F3 n` soft release level, `F5 x` soft release off (x 0), `F4 x` rest key-off, `F0` `F1` `F2` skipped | same | skipped (1 or 2 bytes) | 105 / 33 / 0 |

Loops: the driver keeps them on a stack (`$41DEC`, `$41E0C`, `$41E50`) and reads neither `$F5`'s nor
`$F6`'s offset; the offsets are still exact in every song (1192 `$F6` backs to their `$F5`, 283 `$FE`
offsets past their `$F6`), so SoR1's `loop_end` / `loop_exit` read them as they are.  Depth ≤ 4.
No loop pointer lands inside a loop body.

### 1.4 How it plays

- **FM volume:** TL = table[clamp(v - 2, 0, 19)] on the carriers (the voice's own carrier TLs never
  play); `$FB` moves the step.  Songs use v `$04`-`$13` (`$10` most).
- **Gate:** FM: a note no longer than q keys off after **1 frame** (`$40F72`: `d ≤ q -> 1`); PSG:
  it plays whole (`$4213C`); 75 FM and 146 PSG note bytes are no longer than their gate.
- **Soft release** (`$FF $F3 n`, 18 songs): at the gate the FM note is not keyed off but drops to
  TL = table[(step + n) >> 1] (`$40EE0`) and rings through the rests; the next note keys off, then on.
  PSG: att = ((v - n) >> 1) ^ 15 (`$42076`).  `$FF $F5 0` ends it.
- **Ties:** every note keys off at its end, the next keys on; a note followed by `$FD` is not keyed
  off and the next one slides legato (frequency only).  The tie bit survives the loop jump (none of
  the 28 songs ends a track tied).
- **Vibrato** (`$40E08`): after the delay, every `speed` frames a step of `depth`; the counter starts
  at count / 2 and a half cycle is count steps (SoR1: count + 1).  Restarted on each note unless it
  is a tied note of the same pitch (`$40F3C`); `$F2` forces the restart.
- **FM3 special mode, LFO:** as Streets of Rage's (`$42002`: `$1A` = `$40` on FM3 when an offset is
  nonzero; `$41F0E`).  Data: `$F7 64 00 00 00` / `00 00 00 00`, `$FC` with FMS 2-7.
- **`$FA` writes:** TL `$4x` 464, AM/D1R 29, D2R 8, D1L/RR 38, KS/AR 2, `$26` 20 (Timer B: inert, the
  driver runs on V-int).  Written after the volume, so a carrier's TL holds until the
  next volume change (SoR1 had none on carriers; which of these 464 are carriers: Phase 3).
- **PSG pitch:** divider = table[s] >> octave, clamped 1-1023; octave o plays C(o+1) (o3 = C4, one
  octave above FM's o3).  Dividers differ from Sonic 1's by ±1 (C3: 855 vs 854).  Octave 1 (6 notes,
  songs `$05`, `$15`): one, `$10`, clamps to 1023.
- **PSG detune and vibrato:** (detune + vibrato) >> octave added to the divider: a constant
  interval across octaves (SoR1: a fixed `>> 4`).
- **PSG envelope** (`$422F8`, on from `$F0`/`$FA`): level 0-255 from AL; attack + AR to 255, decay − DR
  to SL, sustain − SR to 0; at the gate release − RR a frame.  att = ((level × (v + 1)) >> 8) ^ 15.
  Presets used: 0 (flat: no envelope), 1-4.
- **PSG noise:** `$F7 n` (n ≠ 8) moves the track onto the noise channel (`$E4 | rate`, white, fixed
  rate 0-2; never clocked by tone 3), its tone left unwritten; `$F7 8` moves it back.  Used on PSG1
  and PSG3 (18 notes); every rate is 0 (`$F8 0`, `$F8 10`).
- **Drum track:** a note plays sample `$80 + n` (`$F0 n`), its note byte unread; a tie skips the
  trigger.  **Rests and gates do not cut** (SoR1 wrote the empty `$85`).  Songs use n 1-3 only.
- **DAC** (the Z80 player, SoR1's descendant: 160 of its 439 code windows of 8 bytes are SoR1's):
  table at Z80 `$01BF`, 5 bytes (start.w size.w pitch.b, LE); `$81` `$1D3`/`$5A5`, `$82` `$778`/`$82B`,
  `$83` `$FA3`/`$831` (pitch 4), `$84` `$17D4`/`$71D` (pitch 18); `$85` reads the first sample's bytes
  (unused).  Format as SoR1's (the sample's own 16-byte delta table, nibble 0 a run), new samples.
  From `$86`: ROM banks (`$0010` sounds per bank, `$1FF8`/`$1FFA`): voice clips (music never).

---

## 2. How close it is to Streets of Rage

**The track language is the same; the engine, the container and the PSG are not.**  SoR1 is SMPS
68k's engine taught MUCOM's track bytes (its songs converted to an SMPS-like header); SoR2 runs
MUCOM88's own model on the 68k with MUCOM88's own song images.

| | Streets of Rage (`smps68k_mucom`) | Streets of Rage 2 (Mucom-MD 0.85) |
|---|---|---|
| engine | SMPS 68k: RAM `$FFF000`, `$30`-byte tracks, Sonic 1's queue | Mucom-MD: RAM `$FFA000`, `$A0`-byte tracks, command words via `jsr $40008` |
| index | 17 absolute longs from `$81` | relative longs from 0: 28 songs, 55 SFX |
| header | `voices.w fm.b psg.b`, entries with volume (PSG: envelope) | MUCOM88 image: 11 channels × (ptr, loop), no volumes |
| loop | `$FF w` jump (clears an FM tie) | `$00` + the header's loop pointer (keeps the tie) |
| notes, rests, `$00`, `$FD` | | **same** |
| `$F0` `$F2` `$F3` `$F4` `$F5` `$F6` `$F7` (FM) `$FA` `$FB` `$FC` (FM) `$FE` | | **same bytes and operands** |
| `$F1` FM | TL = table[v] + header volume (steps to −4) | table[clamp(v − 2, 0, 19)]; the same 24-byte table, 4 entries later |
| `$F8` pan | 1 L, 2 R (table `$7353A`) | 1 R, 2 L (`ror.b #2`); check with a rip |
| `$F9` | pause toggle (FM) / PSG voice | skipped (1 byte) |
| `$FF` | jump | prefix: soft release |
| FM gate, d ≤ q | plays whole | 1 frame |
| voices | 25 bytes, register order, FB/ALG last, carrier TL from volume | **same** |
| FM octave | `$284`-`$4C0`, block = octave | **same table** |
| FM3 special, LFO | | **same** |
| vibrato | half cycle count + 1 | starts at count / 2, half cycle count; PSG depth × 266/256 |
| PSG pitch | Sonic 1's table rows 2-6, detune `>> 4` | one octave `>> octave`, own dividers, detune `>> octave` |
| PSG level | SMPS envelopes (`$81` hold, `$80` restart, `$83` mute), att additive | MUCOM ADSR (AL AR DR SL SR RR) with a release, att = level × volume |
| PSG noise | `$F7`: PSG3 white at tone 3's rate | `$F7 n`: any tone track takes the noise channel, fixed rates |
| drum track | rests and gates cut (`$85`) | they do not |
| DAC | Kosinski Z80 program; `$81`-`$84` | raw copy; same format, new samples `$81`-`$83` used; banked clips |

Reuse: every walk feature Phases 2 and 5 of SoR1 built (volume steps, detune add, gate as note +
rest, voice patches, `LOOP_EXIT`, FM3 special mode, LFO, `modulation_slides`, unequal loops,
`replay_tie`) and its loop / detune / vibrato / register-write readers.  New: the container (§ 3.1.2),
soft release, the PSG model, noise takeover, the vibrato count.

---

## 3. What the code must change

Rules: `agents.md`; `docs/smps_variants.md` § Adding a variant; nothing outside the driver's folder
names it (CLAUDE.md, "Scalable structure").

### 3.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. **The MUCOM track code shared.**  `smps68k/mucom/grammar.py` holds both the language (`d n`, rests,
   loops, detune, vibrato, register writes) and SoR1's departures (PSG rows, pan table).  Split: the
   language to a module both drivers import (where: D1), SoR1's departures stay.  What differs per
   driver is data: the pan mapping, PSG note decoding, flag lengths per kind, the vibrato count.
2. **Tracks that loop by the header.**  `$00` ends a track; where it goes is in the header.
   `RomHeader` gains each track's loop target (start -> loop address); `decode_tracks` turns the
   track's end into a `JUMP` to that label (or `STOP` with none).  The grammar keeps returning `STOP`.
3. **Sound IDs from 0.**  Mucom-MD numbers songs `$00`-`$1B` (`$01nn`); check that `rom_import.py`,
   `song_dump.py --only`, `convert.py --rom-song`, `SoundIndex.address`'s messages and the config
   loader take IDs below `$81` (D5).
4. **The driver's own PSG table and octave range.**  The IR names PSG notes by Sonic 1's table
   (`nC0` = C3); SoR2's dividers differ by ±1 and reach octave 1 (C2).  `PlaybackRules.psg_frequencies`
   is already per driver: give SoR2 its computed table, and check what assumes `nC0` = C3 (PSG
   synthesis pitch, the pitch audit) before admitting a note below it.

### 3.2 New concepts

| Fact | IR | Resolved in | Downstream |
|---|---|---|---|
| header loop pointers | 3.1.2: `JUMP` at the track's end | header reader, `decode_tracks` | none |
| FM volume `v − 2`, clamped 0-19 | `TrackRules.volume_steps` (exists) + a clamp | walk | none |
| PSG volume 0-15, `$FB` clamped | `SET_VOL` / `ALTER_VOL` with a clamp per kind | walk | none |
| soft release (`$FF F3 n`, `F5`) | gate -> note + its tail at the release level, tied, through the rests (`Cxx`) | walk | none |
| FM gate d ≤ q -> 1 frame | `TrackRules` (gate floor: whole vs 1 frame) | walk | none |
| vibrato count / 2 start, count per half | `TrackRules` (the vibrato's phase) next to `modulation_turn_pause` | walk / timeline | check the `4xy` formula |
| PSG detune and vibrato `>> octave`, depth × 266/256 | `TrackRules.word_shift` per octave | walk | none |
| PSG ADSR envelope with release | new: an envelope (AL AR DR SL SR RR) beside SMPS's byte lists, resolved per (preset, volume) | walk | PSG render: attack / decay / sustain, the release tail after the gate (sustain loop + release slide) |
| noise takeover, fixed rate | `PSG_FORM` per track (white, rate 0-2); the track's notes on the noise channel | walk | psg render (rate 0-2 exists) |
| pan 1 R / 2 L | `PAN` | decoder | none |
| drum rests do not cut | `TrackRules.rest_cuts` False (exists) | rules | none |
| `$FA` carrier TL | a voice patch the next volume change undoes (or refused: D4) | walk (`voice_patch.py`) | none |
| tie survives the loop | `jump_clears_tie` False (exists) | rules | none |

---

## 4. Plan

### Phase 0: before code
- [ ] 0.1 The ROM in `input/roms/` (by the user; gitignored).  Rips: VGMRips' Streets of Rage 2 pack
  in `reference/vgz/streets_of_rage_2/` (by the user; untracked).  Every check after Phase 1 needs them.
- [ ] 0.2 Decisions § 5.
- [ ] 0.3 Snapshot (scratch, not kept): `song_dump.py` of every game, the Streets of Rage minimal MODs
  and derived configs, before 3.1.

### Phase 1: refactors (3.1)
- [ ] 1.1 3.1 items 1-4, one commit each; each gate: regression, tool regression, pytest, ruff,
  pyright, vulture, layers, the snapshot unchanged.

### Phase 2: read the ROM
- [ ] 2.1 The driver (name, folder: D1), pinned by SHA-1 in `games.py`.  Locate by the code that reads
  each table, not addresses: the ID string `Mucom-MD Ver0.85`; `lea $429CE(pc),a0` after the song
  command's bus request (`$404F8`); the volume, octave, PSG and preset tables (`lea (pc)` at `$41C28`,
  `$40F5A`, `$42118`, `$42464`); the Z80 copy (`lea $559C2.l,a1` / `move.w #$1FFF,d0`: `z80_ram`'s copy).
- [ ] 2.2 Header reader (`music_header_reader`): § 1.2; the 9 tracks on their chip channels (G and J
  checked empty); loop targets (3.1.2).  Voices: SoR1's `VoiceLayout` (register order, FB/ALG last,
  `carrier_tl=False`).  Index: relative longs, songs `$00`-`$1B`, SFX listed, not read (D6).
- [ ] 2.3 Flags per kind (§ 1.3) and the grammar through 3.1.1; dropped and reported first: soft
  release, PSG envelopes and noise (Phase 3).  Refused: `$F4` 3-6, `$FF F4`, `$FC` on PSG / drums
  (none used).
- [ ] 2.4 `rom_import.py` lists the 28 songs; `song_dump.py` reads, walks and plays each; read
  snapshot `read_streets_of_rage_2`.
- [ ] 2.5 `tests/core/drivers/.../`: the grammar on hand-built bytes; with the ROM: tables, every song
  on its channels, loop targets, what is dropped.
- [ ] 2.6 Pair songs and rips (`rips.yaml`); `vgm_lift --aspects onset length` on FM: the yardstick.

### Phase 3: the walk
- [ ] 3.1 FM: volume law, the gate's 1-frame floor, soft release, ties across the loop, the vibrato's
  count and restart; `$FA` writes (which TLs are carriers: D4).  Pan's direction from a rip.
- [ ] 3.2 PSG: its table and octave range (3.1.4), detune / vibrato `>> octave`, volume, soft release.
- [ ] 3.3 PSG envelopes: the ADSR as an IR envelope, resolved per (preset, volume); the release tail
  after the gate.  The render: a sustain loop at SL (or the SR decay), the release as a slide (D2).
- [ ] 3.4 Noise takeover: the track's notes on the noise channel at rates 0-2; what the tone channel
  does meanwhile (it is left unwritten: check the rip).
- [ ] 3.5 Against the rips' frame logs (`vgm_frames`): FM pitch, level, voice registers at every
  key-on; PSG pitch and level; `vgm_lift` onsets and lengths.

### Phase 4: DAC
- [ ] 4.1 The sample reader: SoR1's `_RunDpcm` on Z80 `$01BF`'s table (raw copy); `$81`-`$84`
  byte-equal to the rips' banks.
- [ ] 4.2 Rates: recount the loop's cycles (SoR1's counts as the start; this player's `$1FFD` writes
  and bank code moved), the 68k's stall from the rips.
- [ ] 4.3 Rests and gates leave the sample playing (`rest_cuts` False); onsets against the rips.

### Phase 5: convert
- [ ] 5.1 `configs/streets_of_rage_2/`: a minimal config per song (26 with notes), `rips.yaml`.
- [ ] 5.2 `vgm_pitch_audit` clean; `measure_volumes.py --configs configs/streets_of_rage_2`, one pass.
- [ ] 5.3 Slots: SoR1's `$90` ran out (FM3 / LFO copies, detune variants); soft release and the PSG
  envelopes add instruments.  Watch `copy_no_slot` / `detune_no_slot`.
- [ ] 5.4 Regression cases chosen by coverage (D7); `tool_regression`: `read_`, `frames_`, `glitches_`.
- [ ] 5.5 Listen in the FT2 clone (the user).

### Phase 6: close
- [ ] 6.1 The analysis (§ 1-2, with what the phases found) to its home (`docs/smps_variants.md` or a
  doc of its own: D1); `docs/architecture.md`; CLAUDE.md's index; this plan to `done/`.

---

## 5. Decisions (open, for the user)

1. **Name and place.**  Proposed: `mucom_md` (Mucom-MD 0.85) in a new family `core/drivers/mucom/`, with
   the shared MUCOM track language (3.1.1) there; SoR1 stays `smps68k/mucom` (its engine is SMPS) and
   imports the language across families.  Alternative: the language in `core/rom/` as a second
   grammar beside SMPS's.  The docs' home follows: `smps_variants.md` names SMPS variants only.
2. **PSG envelopes:** exact (an instrument per preset and volume, release rendered) or the flat
   preset 0 for all with levels by `Cxx` first.  Recommended: exact; presets 1-4 cover 51 of 176.
3. **FM gate floor:** a note no longer than its gate plays one frame on every play: the driver's rule,
   not a glitch: play as heard (as Space Harrier II's drums).  Recommended: yes, checked on a rip first.
4. **`$FA` carrier TL writes:** model as voice patches the next volume change ends, or refuse and
   report.  Recommended: model, once Phase 3 counts the carrier ones.
5. **Song numbering:** the driver's own `$00`-`$1B` (`rom_song: '$05'`), or `$81`-based for
   familiarity.  Recommended: the driver's own.
6. **Music only:** the 55 SFX listed, not read (as SoR1).
7. **Regression:** a few cases by coverage (soft release, the PSG envelope, noise takeover).

Branch: `streets_of_rage_2` (in a worktree with copied data: other sessions share this checkout).
