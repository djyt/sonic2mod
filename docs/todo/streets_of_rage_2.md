# Streets of Rage 2: Mucom-MD, MUCOM88 songs on the 68k

Planned 2026-10-10; reviewed against the code (to `993f45e`) and the rips 2026-11-01.  Goal:
`convert.py` reads Streets of Rage 2's songs from the ROM, as it reads Streets of Rage
(`docs/todo/done/streets_of_rage.md`, `docs/smps_variants.md` § Streets of Rage).  Nothing
implemented yet; this is the probe and the plan.

Status key: `[ ]` open, `[x]` done.  Four parts: what the driver is (1), how close it is to
Streets of Rage's (2), what the code must change - its bottlenecks, refactors, new concepts (3),
the plan (4).  The user's decisions: § 5.

---

## 1. Analysis (probe 2026-10-10)

`Streets of Rage 2 (USA).md`, `GM MK-1054 -00`, 2 MB, SHA-1 `8b656eec9692d88bbbb84787142aa732b44ce0be`
(in `input/roms/`, SHA-1 checked 2026-11-01).  Read with capstone (68k) and z80dis (Z80) in scratch;
every song walked by a scratch reader of the grammar below.  Today `rom_import.py` stops with "no
single driver reads this ROM's songs" (Streets of Rage's driver: none of its PSG-row anchor).

Rips: VGMRips' pack 1.11 in `reference/vgz/streets_of_rage_2/`, 24 tracks: 21 heard in the game, two
unused bonus tracks (22 Go Straight (Beatmix), 24 Little Money Avenue) and 23 Walking Bottom from the
beta (it may have no song in this ROM).  The pack notes the hi-hats play at twice their rate (recorded
at 22 kHz).  `vgm_analyze` reads them.  Not yet paired with songs; nothing below is checked against them.

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
| `$FF s ...` | prefix ($41B94): `F3 n` soft release level, `F5 x` soft release off (x 0) or on, `F4 x` rest key-off, `F0` `F1` (1 byte) `F2` (2) skipped | the same handler | skipped ($4287A; 1 or 2 bytes) | 105 / 33 / 0 |

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
  octave above FM's o3).  Dividers differ from Sonic 1's by ±1 (C3: 855 vs 854).  The playable run is
  o1 A (1016, A2) to o7 B (14, B8), 75 notes: a table that starts at A2, as Space Harrier II's
  (`PsgTable`).  o1 C-G# (and o0) clamp to 1023, 12 c under A2: one note, `$10`, of the 6 in octave 1
  (songs `$05`, `$15`).
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

| | Streets of Rage (`smps68k_streets_of_rage`) | Streets of Rage 2 (Mucom-MD 0.85) |
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

### 3.1 Bottlenecks (review 2026-11-01)

What in today's architecture Streets of Rage 2 runs into, and The Revenge of Shinobi
(`docs/todo/revenge_of_shinobi.md`) with it.  The answer column names the refactor (3.2) or phase.

| # | Bottleneck | Where | Also | Answer |
|---|---|---|---|---|
| B1 | Code two families share has no home: the contracts "drivers: none imports another" and "the families independent" keep SoR1's MUCOM language, run-DPCM format and PCM-table finder (`smps68k/streets_of_rage/`, `smps68k/dac.py`) from a driver outside `smps68k` | `pyproject.toml` | RoS | R1, R2 |
| B2 | A track ends at `$00`; where it goes is the header's.  `decode_tracks` knows no track's end target | `core/rom/tracks.py` | | R3 |
| B3 | `TrackRules`: 12 flat switches, each a branch in `DriverTrack` / `code.py`; SoR2 adds ~6 (gate floor, soft release, vibrato phase, PSG depth scale, word shift by octave, volume clamp) | `core/smps/rules.py` | every driver | R4 |
| B4 | The key-off yields notes only (`DriverTrack.cut`): soft release is a level change inside a ringing note, and rests that do not cut | `core/smps/driver_track.py` | | R5 |
| B5 | Noise is PSG3's mode, permanent: `Track.in_noise_mode`, `DriverState`, `noise_derive`, `synth_roots`, CLAUDE.md's rule; the lift (`noise_mode`: PSG3 the whole song) and `frame_check` (pitch from tone 3).  SoR2: any tone track, and back, at a fixed rate | `core/smps`, `core/plan`, `core/vgm/lift`, `core/audit` | RoS: a 4th track | R6 |
| B6 | `PsgEnvelope`: attenuation steps added to the volume, no release.  SoR2: ADSR, level × volume, a release after the gate.  Downstream PSG notes keep their cuts; release slides are FM's (`Fades.release`) | `core/smps/driver_tables.py`, `core/convert/channel_writer/fades.py` | | Phase 3.3 (new concept) |
| B7 | 31 instruments: SoR1 already hit `copy_no_slot` / `detune_no_slot`; envelope × volume and soft-release tails add more | `core/plan`, `core/merge` | | measured in 2.7 before 3.3's design (D2) |
| B8 | Framework names say SMPS (`SmpsVariant`, `SmpsDriver`, `HeaderLayout`); Mucom-MD is the first engine that is not | `core/rom/variant.py`, `core/drivers/names.py` | | no rename: it fits through the hooks (`grammar`, `music_header_reader`, `voice_reader`, `rules_from_rom`, `dac`); a line in the docs.  Revisit at a third engine |
| B9 | Z80 program loads are enumerated shapes; SoR2's raw copy (`lea $559C2.l,a1` / `move.w #$1FFF,d0`) may be a sixth | `core/rom/z80.py` | | 2.1 |
| B10 | `vgm_analyze`'s "smps" column names PSG notes by Sonic 1's table (nC0 = C3) | `tools/vgm/` | | cosmetic: read the real-pitch column |

Already scales, no change: the lazy registry and SHA-1 pinning (`registry.py`, `games.py`), trial
detection held by the driver contract (`tests/core/drivers/test_contract.py`), coverage-selected
regression (`tests/selection.py`), the driver's own PSG table (`PsgTable`, `b0dc7b0`: the old
3.1.4), sound IDs below `$81` (R7: checked).

### 3.2 Refactors (behaviour unchanged, every baseline byte-identical)

1. **R1 Track languages as a package, MUCOM's in the framework** (D1).  `grammar.py` splits; the
   decoder stays one, for every language:

   ```
   core/rom/
     tracks.py          the decoder: follows code with any grammar (unchanged)
     grammar/
       __init__.py      Instruction, track_label: what every language yields
       smps.py          SMPS: $81-$DF notes, $E0+ flags (today's grammar.py)
       mucom.py         MUCOM: d n notes, $F0+ flags, loops, MucomDialect
     flags.py           FlagSpec vocabulary (both languages)
   core/drivers/
     smps68k/streets_of_rage/     SoR1: its dialect and departures
     mucom/md/          SoR2: its dialect, header, memory
   ```

   MUCOM's language: `d n` notes, rests, `$00`, `$F5` / `$F6` / `$FE` loops, `$F2` detune, `$F4`
   vibrato, `$FA` writes, `$FD`.  What differs per driver is data, a `MucomDialect`: the pan table,
   PSG note decoding, the vibrato count's bias, `$FF` (jump or prefix).  SoR1's `grammar.py` keeps
   its departures.  RoS's 5.1.1 is this step.  `core/rom`'s layer contract names the package.
2. **R2 Z80 PCM to the framework: `core/rom/pcm/`** (a package: the table in `__init__`, the formats by
   player, `smps.py` and `sor.py`) gains SoR1's `_RunDpcm` (as `sor.RunDpcm`) and
   `smps68k/dac.py`'s `pcm_table` (`ld iy,nn`); the 68k family keeps its pitch tables.
3. **R3 Header loops.**  `RomHeader.loops`: each track's start -> its loop target (none: `STOP`
   stays).  `decode_tracks` carries each instruction's track start and turns a `STOP` reached from
   it into a `JUMP`; code two tracks share up to a `$00` with different targets is an error.
   No driver sets it yet.
4. **R4 `TrackRules` grouped by concern** (D9): volume (steps, clamp), gate (floor, spares tied,
   sees tie, tied rest holds), ties (jump clears, envelope restart), modulation (turn pause, phase,
   depth scale), PSG (word shift, noise writes tone 3).  `_REVIEWED` compares the nested departures.
5. **R5 Key-off as events.**  `DriverTrack.cut` returns events (notes and level changes);
   `code.py` emits them as they come.
6. **R6 Noise reversible, on any PSG track.**  A `PsgForm` with no form: tone again.
   `Track.in_noise_mode` by the current form; `DriverState` back on `psg_voice_map`'s routing;
   `noise_derive` and `synth_roots` by note, not channel; `frame_check`: a fixed-rate noise note
   has no tone-3 pitch; the lift: noise by the track that owns it.  CLAUDE.md's "Noise mode is
   permanent" becomes "until the driver turns it back".  RoS's noise track (5.1.3) builds on it.
7. **R7 Sound IDs from 0.**  Checked: `:02X` formatting, `parse_number`, `read_index(first_id)`,
   music and SFX IDs disjoint.  Left: help and messages that say "$81 ..."; a test reading ID 0.

Order: R1, R2, R7 before Phase 2; R3 with 2.2; R4, R5, R6 before Phase 3.  One commit each.

### 3.3 New concepts

| Fact | IR | Resolved in | Downstream |
|---|---|---|---|
| header loop pointers | R3: `JUMP` at the track's end | header reader, `decode_tracks` | none |
| FM volume `v − 2`, clamped 0-19 | `volume_steps` over every byte + R4's clamp on `$FB` | walk | none |
| PSG volume 0-15, `$FB` clamped | `SET_VOL` / `ALTER_VOL` with a clamp per kind | walk | none |
| soft release (`$FF F3 n`, `F5`) | R5: gate -> note, level change, tied tail through the rests | walk | `Cxx` on a tie (exists) |
| FM gate d ≤ q -> 1 frame | R4's gate floor | walk | none |
| vibrato count / 2 start, count per half | R4's modulation phase | walk / timeline | check the `4xy` formula |
| PSG detune and vibrato `>> octave`, depth × 266/256 | R4's word shift by octave, depth scale | walk | none |
| PSG pitch | the driver's table, A2-B8 (§ 1.4); o1 C-G# -> index 0 in the grammar (D8) | variant, grammar | none |
| PSG ADSR envelope with release | `PsgEnvelope` gains a release; resolved per (preset, volume) in the walk (D2) | walk | PSG render: attack / decay / sustain; PSG release slides beside FM's (B6) |
| noise takeover, fixed rate | R6: `PsgForm($E4 \| rate)` and back | walk | psg render (rates 0-2 exist) |
| pan 1 R / 2 L | `PAN` | dialect (R1) | none |
| drum rests do not cut | `rest_cuts` False (exists) | rules | none |
| `$FA` carrier TL | a voice patch the next volume change undoes (D4; RoS's too) | walk (`voice_patch.py`) | none |
| tie survives the loop | `jump_clears_tie` False (exists) | rules | none |

---

## 4. Plan

Each step a commit on branch `streets_of_rage_2` (in the main checkout, while no other session
commits there).  Gate for every refactor: `python tools/dev/check.py --regress` (regression, tool
regression, pytest, ruff, pyright, vulture, layers) and the 0.4 snapshot unchanged.

### Phase 0: before code
- [x] 0.1 The ROM in `input/roms/`; rips in `reference/vgz/streets_of_rage_2/` (2026-11-01).
- [x] 0.2 Decisions § 5 (2026-11-01).
- [x] 0.3 The branch, off `a704100`; both suites' baselines from `86831cf` (byte-identical commits
  since), the tree passing them.
- [x] 0.4 Snapshot (scratch, not kept): every game's songs are `tool_regression`'s `read_` cases;
  Streets of Rage's derived configs (`--show-config`, 16 songs) dumped to scratch.

### Phase 1: refactors for reading (3.2)
- [x] 1.1 R1: `core/rom/grammar/` (`smps.py`, `mucom.py`), SoR1 a dialect.
- [x] 1.2 R2: `RunDpcm`, `pcm_table` to `core/rom/pcm/` (a package by player: `smps.py`, `sor.py`).
- [x] 1.3 R7: sound IDs from 0 (messages, a test).

### Phase 2: read the ROM
- [x] 2.1 The driver `mucom_md` in `core/drivers/mucom/md/` (D1): `names.py`, `registry.py`,
  `games.py` (SHA-1), `pyproject.toml`'s three driver contracts (registry-only, families
  independent, drivers independent), `tests/roms.py` `GAMES`, `_REVIEWED`.  Located by the code that
  reads each table: the ID string `Mucom-MD Ver0.85`; `lea $429CE(pc),a0` after the song command's
  bus request (`$404F8`); the volume, octave, PSG and preset tables (`lea (pc)` at `$41C28`,
  `$40F5A`, `$42118`, `$42464`); the Z80 copy (B9).
- [x] 2.2 Memory (LE words, relative to the image's +9) and the header reader (§ 1.2): the 9 tracks
  on their chip channels (G and J checked empty); loop targets with R3.  Voices: SoR1's
  `VoiceLayout`.  Index: relative longs, songs `$00`-`$1B`, SFX listed, not read (D6).
- [x] 2.3 Flag tables per kind (§ 1.3) and its dialect (R1).  Dropped and reported until Phase 3:
  soft release, PSG envelopes, noise.  Refused: `$F4` 3-6, `$FF F4`, `$FC` on PSG / drums (none used).
  D4 done here (every song must walk): `$FA` queues up to 8 writes, written after the volume each
  frame (`$41996`), so a carrier's TL holds until the next volume change or voice
  (`TrackRules.carrier_writes_hold`; `SmpsVoice.level_fixed`; a volume change in the same read is
  refused, none happens).  `$02` `$12` `$13` make 18 such voice copies each (slots: 2.7).
- [x] 2.4 `rom_import.py` lists the 28 songs; `song_dump.py` reads, walks and plays each; tool case
  `read_streets_of_rage_2`.
- [x] 2.5 Tests: the dialect on hand-built bytes; with the ROM: tables, every song on its channels,
  loop targets, what is dropped.  The driver contract passes.
- [ ] 2.6 `configs/streets_of_rage_2/rips.yaml`: 24 rips against 26 songs with notes (D10);
  `vgm_lift --aspects onset length` on FM: the yardstick.
- [ ] 2.7 Slot census: every song converted as read (minimal configs), `copy_no_slot` /
  `detune_no_slot` counted, the envelope instruments (D2's table) added: which songs need banking.

### Phase 3: the walk
- [ ] 3.0 R4, R5, R6, a commit each.
- [ ] 3.1 FM: volume law (the clamp is on the stored step: `$FB` after an out-of-range `$F1`), the
  gate's 1-frame floor (D3, checked on a rip), soft release, ties across the loop, the vibrato's
  phase and restart; `$FA` carrier writes against a rip (D4, modelled in 2.3).  Pan's direction
  from a rip.
- [ ] 3.2 PSG: its table (§ 1.4, D8), detune / vibrato `>> octave`, volume, soft release.
- [ ] 3.3 PSG envelopes: the ADSR resolved per (preset, volume), with its release (D2).  The render:
  a sustain loop at SL (or the SR decay); the release a PSG slide beside `Fades.release`.
- [ ] 3.4 Noise takeover (R6): the track's notes on the noise channel at rates 0-2; what its tone
  channel does meanwhile (unwritten: check a rip).
- [ ] 3.5 Against the rips' frame logs (`vgm_frames`): FM pitch, level, voice registers at every
  key-on; PSG pitch and level; `vgm_lift` onsets and lengths.

### Phase 4: DAC
- [ ] 4.1 The sample reader: R2's `RunDpcm` on Z80 `$01BF`'s table; `$81`-`$84` byte-equal to the
  rips' banks.
- [ ] 4.2 Rates: recount the loop's cycles (SoR1's counts as the start; this player's `$1FFD` writes
  and bank code moved), the 68k's stall from the rips (their hi-hats play 2× fast: pack notes).
- [ ] 4.3 Rests and gates leave the sample playing (`rest_cuts` False); onsets against the rips.

### Phase 5: convert
- [ ] 5.1 `configs/streets_of_rage_2/`: a minimal config per song with notes (26).
- [ ] 5.2 `vgm_pitch_audit` clean; `measure_volumes.py --configs configs/streets_of_rage_2`, one pass.
- [ ] 5.3 Slots against 2.7's census: `copy_no_slot` / `detune_no_slot` none; where short, envelope
  instruments banked (D2), then the per-preset fallback.
- [ ] 5.4 Regression cases chosen by coverage (D7); `tool_regression`: `read_`, `frames_`, `glitches_`.
- [ ] 5.5 Listen in the FT2 clone (the user).

### Phase 6: close
- [ ] 6.1 The analysis (§ 1-2, with what the phases found) to its home (D1: a doc of its own,
  `docs/mucom_drivers.md`, since `smps_variants.md` names SMPS variants); `docs/architecture.md`;
  CLAUDE.md's index; this plan to `done/`.

---

## 5. Decisions (the user, 2026-11-01)

1. **Name and place:** `mucom_md` (Mucom-MD 0.85) in a new family, `core/drivers/mucom/md/`; SoR1
   stays `smps68k/streets_of_rage` (its engine is SMPS's).  The shared MUCOM language in the framework, with a
   clear split from the decoder: `core/rom/grammar/` a package, `smps.py` and `mucom.py` in it,
   `tracks.py` the one decoder (R1's tree).
2. **PSG envelopes: exact**, an instrument per (preset, volume), identical curves sharing one,
   release rendered; **banked** where a song runs out of slots, the per-preset fallback (its
   commonest volume, `Cxx` elsewhere) only after that.  The size, from the ROM (scratch walk of
   every PSG track, loops unrolled, one play; `$FA` = a song's own envelope):

   | Envelope instruments (preset ≠ 0) | Songs |
   |---|---|
   | 0 | `$07` `$08` `$0A` `$14` `$15` `$16` `$17` `$19` |
   | 1 | `$00` `$01` `$0E` `$0F` `$13` `$18` `$1B` |
   | 2-3 | `$03` `$04` `$06` `$11` `$1A` |
   | 4-5 | `$02` `$09` `$0B` `$0D` `$12` |
   | 7 | `$05` (preset 3 at volumes 7-13) |

   Preset 0 (flat) is one instrument a song, its levels `Cxx`; noise one or two (`$0B` `$12` `$19`).
   35 distinct curves over the game.  So 1-8 PSG instruments a song, against SoR1's few: the slots
   that matter are FM's (2.7).
3. **FM gate floor: as heard**, a note no longer than its gate plays one frame; checked on a rip first.
4. **`$FA` carrier TL writes: modelled**, a voice patch the next volume change ends (RoS's too).
5. **Song numbering:** the driver's own, `$00`-`$1B` (`rom_song: '$05'`).
6. **Music only:** the 55 SFX listed, not read.
7. **Regression:** a few cases by coverage (soft release, a PSG envelope, noise takeover).
8. **PSG octave 1 C-G#** (one note, `$10`): read as A2, the table's first note: the clamp as heard
   (divider 1023; A2's 1016 is 12 c sharp of it).
9. **`TrackRules` grouped** by concern (R4) before SoR2's switches.
10. **Rips without a song** (23 Walking Bottom, beta; maybe the two bonus tracks): paired if a ROM
   song matches by FM onsets, else left out of `rips.yaml` (named in its comment).
