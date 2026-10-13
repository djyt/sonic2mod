# The Revenge of Shinobi: Streets of Rage's driver, an earlier version

Planned 2026-10-12.  Goal: `convert.py` reads The Revenge of Shinobi's songs from the ROM, as it reads
Streets of Rage (`docs/smps_variants.md` § Streets of Rage, `docs/todo/done/streets_of_rage.md`).
Nothing implemented yet; this is the probe and the plan.

Status key: `[ ]` open, `[x]` done.  Parts: the ROMs (1), what the driver is (2), how close it is
to Streets of Rage's (3), the prototype (4), what the code must change (5), the plan (6).  Open
decisions for the user: § 7.

---

## 1. The ROMs: one sound driver in all five

`input/roms/` holds five files.  The sound region, `$74770`-`$80000` (music index, envelopes, the
68k driver, every song, the SFX), is **byte-identical in all five, at the same addresses**.  So are
the Z80 player (`$769B2`-`$78931`) and its loader (`$3DED8`).  The files differ only below `$745E2`
(game code and graphics: about 130 KB between the 1989 builds and Rev B, 2.2 KB between Rev B and its
Virtual Console copy).

| File | Serial | SHA-1 |
|---|---|---|
| `Revenge of Shinobi, The (USA, Europe).md` | `GM 00001105-01` | `a88546e159bc882bf05eba66c09a3b21dee4154e` |
| `Revenge of Shinobi, The (USA, Europe) (Rev A).md` | `GM 00001105-02` | `76af6ca97df08f2370f3a7fc2311781a0f147f4b` |
| `Revenge of Shinobi, The (USA, Europe) (Rev A) (Beta) (1990-02-01).md` | `GM 00001105-02` | `80865449e5edcfc637c726441f5084d5db105f5b` |
| `Revenge of Shinobi, The (USA, Europe) (Rev B).md` | `GM 00001105-03` | `7ed6f142884a08bb3c07a2e6e35405440343502f` |
| `Revenge of Shinobi, The (World) (Rev B) (Virtual Console).md` | `GM 00001105-03` | `b1044e3b782351e69eba0abc94a3f08c379c27bc` |

All five are 512 KB, header date `(C)SEGA 1989.SEP`.  Configs name Rev B (the user's choice of
file); `games.py` can pin all five SHA-1s to the one driver.  Rips: VGMRips' pack (*The Super
Shinobi* / *The Revenge of Shinobi*, 1.06, 19 tracks) in `reference/vgz/revenge_of_shinobi/`.  It
does not say which revision it was recorded from; with one sound region, that does not matter.

---

## 2. Analysis (probe 2026-10-12)

Read with capstone (68k) and z80dis (Z80) in scratch.  `docs/audits/rom_survey.md` § 1 listed it as
a near miss: "MUCOM's code patterns match, but its music index points at no header".  The cause is
its header (§ 2.2); everything Streets of Rage's driver finds by code (`mucom/variant.py`: the
`subi.b #$81` music index, `subi.b #$A0` SFX index, the FM octave, the PSG rows, the
`ext.w d3` volume table) is found once in Shinobi too.

### 2.1 Where things are

| What | ROM | Note |
|---|---|---|
| Driver entry | `$78932` | from V-int; no tempo: every track is updated every call (a tick a frame) |
| Music index | `$74770` | absolute longs; IDs `$81`-`$9F` reach music (`cmpi.b #$A0`), 19 songs `$81`-`$93`, `$94`-`$9F` all point at `$92`'s header |
| SFX index | `$7F43C` | `$A0`-`$CF`: not read (music only) |
| PSG envelopes | `$747EC` | longs, 1-based (`$B(a3)` 0 = none); `$80` restart, `$81` hold, `$83` silence: Streets of Rage's commands |
| FM octave | `$79318` | `$284`-`$4C0`, block = octave: Streets of Rage's table |
| FM volume | `$795F8` | TL by step, `36 33 30 2D` before it (steps -4..-1): Streets of Rage's |
| Carrier masks | `$796D0` | `08 08 08 08 0C 0E 0E 0F` |
| PSG table | `$799AC` | rows of 12; Sonic 1's dividers, rows repeated as Streets of Rage's |
| Flag tables | `$79348` FM and drums, `$79388` PSG | `tst.b $1(a3)` picks one (PSG channels have bit 7 set) |
| Track RAM | `$FFF740` | `$30` bytes: FM1-FM5, drums `$FFF830`, PSG `$FFF860` `$890` `$8C0`, noise `$FFF8F0`; SFX from `$FFF920` |
| Z80 player | `$769B2` | uncompressed, two blocks: `$157` bytes to Z80 `$0000`, `$1E28` from `$76B09` to Z80 `$0160` (`$3DED8`) |

### 2.2 Song header (`$78ED6`)

```
+0  voices.w      relative to the header
+2  fm.b          5 or 6 (the 6th is the drum track)
+3  psg.b         0-4
+4  $01           -> each track's +2: never read
+5  song volume   -> each track's +3: added to every FM carrier TL (2-14; 6 commonest)
+6  FM entries, 4 bytes: ptr.w, $F4 (-> +8: never read), $00 (-> +9: the TL before any $F1)
    PSG entries, 6 bytes: ptr.w, $F4 (never read), att.b (+9: before any $F1), ?.b (+A: 0 or 2), envelope.b (+B)
```

Shaped like Sonic 1's (`smpsHeaderTempo`, a transposition per track), but nothing reads those bytes:
no tempo, no transposition.  PSG tracks load in order `$80` `$A0` `$C0` `$E0`: PSG1, PSG2, PSG3, and
a 4th, the noise track (§ 2.4).

### 2.3 Track code

The same language as Streets of Rage (`mucom/grammar.py`): `d n` a note (`d` `$01`-`$7F` frames,
`n` octave << 4 | semitone), `$80|d` a rest, `$00` the end, `$F0`-`$FF` a flag.  Flags:

| Byte | FM | PSG | Drum track |
|---|---|---|---|
| `$F0 n` | voice n, **1-based** (0 and 1 both the first) | envelope n | sample `$80 \| n` |
| `$F1 v` | volume step: TL = table[v] + song volume | att = -v & 15 (no header volume) | (FM's) |
| `$F2 lo hi` | detune word, **2 bytes**, sets | **divider + 1** when hi is not 0, else + 0 | (FM's) |
| `$F3 n` | gate | gate | no effect |
| `$F4 0 d s w c` / `1` / `2` | vibrato, as Streets of Rage | **5 bytes skipped**: no PSG vibrato | (FM's) |
| `$F5` / `$F6 x n back.w` / `$FE o.w` | loops, as Streets of Rage | same | same |
| `$F7 a b c d` / `$F7 x` | FM3 special mode, as Streets of Rage | this track drives the noise channel: `$E4` (white, **fixed rate N/512**), tone 3 silenced | (FM's) |
| `$F8` | **stop** (as `$00`) | 1 byte, nothing | (FM's) |
| `$F9 n` | **pan**: `ror.b #2`: 1 R, 2 L, 3 C, 0 none | **stop** | (FM's) |
| `$FA r v` | YM register write; on a carrier TL, v + song volume | envelope (as `$F0`) | (FM's) |
| `$FB n` | volume step += n | att -= n | (FM's) |
| `$FC f p a` | LFO, as Streets of Rage | hangs the driver | (FM's) |
| `$FD` | tie | tie | tie |
| `$FF hi lo` | jump (big-endian, relative: Sonic 1's); **keeps the tie** | same | same |

### 2.4 How it plays (what differs from Streets of Rage)

- **FM volume:** carrier TL = table[step] + the song's volume (header byte 5), one value for every
  track; Streets of Rage adds each track's own header volume.
- **PSG:** att = the `$F1` level + the envelope's step, clamped to 15.  No vibrato, no detune shift:
  `$F2` only adds 0 or 1 to the divider.  Opening's PSG1 and PSG3 play in unison a divider apart
  (226 / 227 in the rip): a chorus.
- **The noise track** (`$FFF8F0`, `$79802`): reads notes, writes no frequency, writes the noise
  channel's attenuation.  The noise control is `$E4` from init: white noise at the fixed N/512 rate,
  never clocked by tone 3.  A tone track's `$F7` does the same to itself.
- **Drum track:** rests write nothing and the gate does nothing, so **nothing cuts a sample**.
  Streets of Rage's rest wrote the empty `$85`, and its gate bug silenced hits; neither exists here.
  The note byte of sample `$85` sets its pitch: `(n >> 1) + 4` (+ 8 from `n = $10`), written to the
  Z80's table (`$019B`, `$85`'s pitch byte) before the trigger.
- **DAC** (the Z80 player): **not** Streets of Rage's.  Sonic 1's DPCM (the 16-entry delta table
  `00 01 02 04 08 10 20 40 80 FF FE FC F8 F0 E0 C0` at Z80 `$0019`), entries of 12 bytes at Z80
  `$0160`: start.w size.w, loop count, loop flag, loop start.w, loop size.w, ?, pitch (LE, from
  `$1FF8`'s base).  Sounds `$81`-`$86` in Z80 RAM; from `$87`, a ROM bank (`$1FF8`/`$1FFA`): voice clips.
- **Track RAM, voices, FM3 special mode, LFO, vibrato, gate, ties:** the same code as Streets of
  Rage's, to be checked against the rips rather than assumed (§ 6, Phase 3).

---

## 3. How close it is to Streets of Rage

About two thirds of the 68k driver is Streets of Rage's instruction for instruction: 820 of 1,229
instructions, normalised for addresses and RAM base.  The load routine, track layout, note read, FM
update, vibrato, loops, voice load and volume table are the same code.  **The track language and
the engine are shared; the header, about ten flag meanings, the PSG and the DAC are not.**

| | Streets of Rage (`smps68k_mucom`) | The Revenge of Shinobi |
|---|---|---|
| sound RAM | `$FFF000` | `$FFF700` |
| header | `voices.w fm.b psg.b`; FM `ptr.w vol.b`, PSG `ptr.w vol.b env.b` | + 2 bytes (song volume); FM 4 bytes, PSG 6 (Sonic 1's sizes) |
| PSG tracks | 3: PSG3, PSG2, PSG1 | up to 4: PSG1, PSG2, PSG3, noise |
| voice numbers | from 0 | from 1 |
| `$F2` | `lo hi mode` (sets / adds) | `lo hi` (sets); PSG: + 0 / 1 |
| `$F8` / `$F9` FM | pan (1 L, 2 R) / pause | stop / pan (1 R, 2 L, 3 C) |
| PSG `$F0` `$F4` `$F7` `$F9` | dropped / vibrato / noise at tone 3's rate / voice | envelope / skipped / noise at N/512 / stop |
| FM volume | step + the track's header volume | step + the song's volume |
| PSG volume | + the track's header volume; vibrato and detune `>> 4` | none of these |
| jump | clears an FM or drum tie | keeps it |
| drum rest / gate | cut (sample `$85`) | nothing |
| DAC | Kosinski player, run-DPCM, 5-byte entries | raw two-block copy, Sonic 1's DPCM, 12-byte entries with loops, `$85` pitched |
| notes, rests, loops, `$F1` FM, `$F3`, `$F4` FM, `$F5` `$F6` `$FE`, `$F7` FM, `$FA` FM, `$FB`, `$FC`, `$FD`, voices, FM tables | | **same** |

Streets of Rage 2's plan (`docs/todo/streets_of_rage_2.md` § 3.1.1) proposes moving the shared
MUCOM track language out of `smps68k/mucom/grammar.py`.  Shinobi is the third reader of that
language, and an argument for doing it.

---

## 4. The prototype (scratch, not kept)

`mucom/variant.py`'s `MUCOM`, `dataclasses.replace`d with: § 2.2's header (a `music_header_reader`,
the 4th PSG track on PSG3's name), § 2.3's flag tables, 1-based voices, `jump_clears_tie` False,
`rest_cuts` False, no DAC.  Song code walked by the existing reader and compared with
`compare_with_rip` (`vgm_lift`'s comparison) on onsets.  Each rip was paired with the decoded song
whose FM onsets it matches best.

- **16 of 19 songs decode**; loops and jumps are where the rips loop.  `$8A`, `$8B` and `$8E` stop
  at an `$FA` write to a carrier TL (`SmpsVoice.patched` refuses: "a TL the track volume is added to").
- **FM onsets: right on 16 rips**, 0-10 wrong a song of 19-910 notes, 10 songs exact.

| Song | Rip | FM onsets wrong / notes |
|---|---|---|
| `$81` | 03 The Shinobi | 6 / 692 |
| `$82` | 04 Terrible Beat | 3 / 529 |
| `$83` | 05 Round Clear | 0 / 52 |
| `$84` | 07 Make Me Dance | 0 / 650 |
| `$85` | 13 Over the Bay | 6 / 910 |
| `$86` | 12 China Town | 0 / 812 |
| `$87` | 09 Run or Die | 0 / 736 |
| `$88` | 08 Like a Wind | 0 / 737 |
| `$89` | 14 Labyrinth | 2 / 152 |
| `$8C` | 10 Ninja Step | 0 / 746 |
| `$8D` | 02 Long Distance | 0 / 378 |
| `$8F` | 18 Silence Night | 0 / 192 |
| `$90` | 17 My Lover | 0 / 419 |
| `$91` | 19 Game Over | 0 / 19 |
| `$92` | 15 The Ninja Master | 10 / 676 |
| `$93` | 01 Opening | 2 / 268 |

  Left: 06 Sunrise Blvd., 11 The Dark City, 16 Failure, against `$8A`, `$8B`, `$8E` (not decoded).  Not
  certainly one to one: `$8E` has no PSG track, Failure's rip has PSG.
- **PSG and drums: not modelled**, and they differ.  Opening's PSG notes change pitch at a held
  attenuation, which the lift does not read as attacks (a lift limit; the pitches are right).  Drums:
  Over the Bay's hits a frame apart in places; Terrible Beat's rip has 94 hits the song lacks
  (sample `$85`'s pitched notes or the loop fields: Phase 4).

---

## 5. What the code must change

Rules: `agents.md`; `docs/smps_variants.md` § Adding a variant; nothing outside the driver's folder
names it (CLAUDE.md, "Scalable structure").

### 5.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. **The MUCOM track language shared** (with Streets of Rage 2: its 3.1.1).  What differs per driver
   is data: the pan mapping, the flag table per kind, `$F2`'s length, PSG note rows.
2. **Header entries past a tempo byte pair that is not a tempo.**  `HeaderLayout.tempo=False` puts
   the entries at +4; Shinobi's start at +6 with no tempo.  Either a `HeaderLayout` field (bytes
   before the entries) or Shinobi's own `music_header_reader` (D2).
3. **A 4th PSG track.**  `header.py`'s `_MAX_PSG_TRACKS = 3`; the IR's PSG channels are PSG1-3 with
   noise as PSG3's form.  A track on the noise channel alone (chip channel `NOISE`) through the walk,
   the planner and the PSG render: check what assumes three (D3).
4. **`z80_loads`:** an uncompressed copy in two blocks (`lea $A00000,a1` / `lea src,a2` /
   `move.w #n,d2` / `move.b (a2)+,(a1)+` / `dbra`), and the bytes the 68k pokes after it
   (`$1FF8`-`$1FFA`).

### 5.2 New concepts

| Fact | IR | Resolved in | Downstream |
|---|---|---|---|
| song volume on every FM carrier | each FM channel header's `volume` = header byte 5 | header reader | none |
| voices from 1 | `SetVoice(n - 1)`, 0 -> 0 | grammar | none |
| pan 1 R / 2 L / 3 C / 0 none | `PAN` | grammar | 0 (both off): how the MOD plays a muted channel |
| `$F8` FM / `$F9` PSG stop | `STOP` | flag tables | none |
| PSG `$F2`: + 0 / 1 divider | `Detune(0 or 1)` with `word_shift` 0 | grammar, `TrackRules` | detune variants |
| no PSG vibrato | `$F4` dropped on PSG | flag table | none |
| fixed-rate noise | `PsgForm($E4)` | grammar | psg render (rate 0 exists) |
| noise track | a PSG track on `NOISE` (5.1.3) | header, walk | planner, render |
| `$FA` carrier TL = v + song volume | a voice patch the next `$F1` / voice ends, or a level change (D4) | walk (`voice_patch.py`) | none |
| jump keeps the tie, drum rests do not cut | `jump_clears_tie` / `rest_cuts` False (exist) | rules | none |
| DAC: Sonic 1's DPCM, looped, `$85` pitched by note | `DacSample`s; `$85` a pitched copy per note | `dac.py` | sample loops (exist) |

---

## 6. Plan

### Phase 0: before code
- [ ] 0.1 Decisions § 7.
- [ ] 0.2 Snapshot (scratch, not kept): `song_dump.py` of every game, the Streets of Rage minimal MODs,
  before 5.1.

### Phase 1: refactors (5.1)
- [ ] 1.1 5.1 items 1-4, one commit each; each gate: regression, tool regression, pytest, ruff,
  pyright, vulture, layers, the snapshot unchanged.

### Phase 2: read the ROM
- [ ] 2.1 The driver (name, folder: D1), the five SHA-1s in `games.py`.  Locate by code, as
  `mucom/variant.py`: its anchors all hold once in Shinobi except `move.b $B(a3),d0` (twice; the
  envelope table's `lea` follows only one).  Music IDs `$81`-`$93` (stop the index at the first
  repeated pointer, or by the driver's bound: D5).
- [ ] 2.2 Header reader (§ 2.2), voices (Streets of Rage's `VoiceLayout`, 1-based), flags per kind
  (§ 2.3) through 5.1.1.
- [ ] 2.3 `rom_import.py` lists the 19 songs; `song_dump.py` reads, walks and plays each; read
  snapshot `read_revenge_of_shinobi`.
- [ ] 2.4 `tests/core/drivers/.../`: the grammar on hand-built bytes; with the ROM: tables, every song
  on its channels, what is dropped.
- [ ] 2.5 `configs/revenge_of_shinobi/rips.yaml` (§ 4's table; the last three once they decode);
  `vgm_lift --aspects onset length` on FM.

### Phase 3: the walk
- [ ] 3.1 FM: the song volume; `$FA` carrier TLs (`$8A` `$8B` `$8E`); the gate's tie rules
  (`gate_sees_tie`, `gate_spares_tied`, `tied_rest_holds`: Streets of Rage's values to be checked
  against `$78BE2` / `$7921A`); ties across the jump; pan's direction from a rip.
- [ ] 3.2 PSG: volume without header, envelopes, the `+1` detune, `$F7` fixed noise, the noise track.
  What is the PSG entry's `+A` byte (0 or 2)?
- [ ] 3.3 Against the rips' frame logs (`vgm_frames`): FM pitch, level and voice registers at every
  key-on; PSG pitch and level.

### Phase 4: DAC
- [ ] 4.1 The sample reader: Sonic 1's DPCM on Z80 `$0160`'s 12-byte table; the loop fields (count,
  start, size); `$81`-`$86` against the rips' banks.
- [ ] 4.2 `$85` pitched by the note (`(n >> 1) + 4`, + 8 from `$10`): a copy per pitch used.
- [ ] 4.3 Rates: count the player's cycles per output (`$0096`-`$00F0`: two nibbles, `DJNZ` on the pitch),
  the 68k's stall from the rips; onsets against the rips (Terrible Beat's extra hits).

### Phase 5: convert
- [ ] 5.1 `configs/revenge_of_shinobi/`: a minimal config per song (Rev B), `rips.yaml`.
- [ ] 5.2 `vgm_pitch_audit` clean; `measure_volumes.py --configs configs/revenge_of_shinobi`, one pass.
- [ ] 5.3 Regression cases chosen by coverage (D6); `tool_regression`: `read_`, `frames_`, `glitches_`.
- [ ] 5.4 Listen in the FT2 clone (the user).

### Phase 6: close
- [ ] 6.1 The analysis to `docs/smps_variants.md` (beside Streets of Rage); `docs/architecture.md`;
  CLAUDE.md's index; `rom_survey.md`'s near-miss line; this plan to `done/`.

---

## 7. Decisions (open, for the user)

1. **Name and place.**  Proposed: `smps68k/shinobi` (`smps68k_shinobi`), a sibling of `smps68k/mucom`
   sharing the track language (5.1.1).  Alternative: one `mucom` package with two variants, since the
   engines are the same code.
2. **The header:** a `HeaderLayout` field for the bytes before the entries (any driver), or Shinobi's
   own reader.  Recommended: the field; with it the entries are `SMPS_FM_ENTRY` / `SMPS_PSG_ENTRY`
   sized, their unread bytes left `None`.
3. **The noise track:** a 4th PSG track on `NOISE` in the IR (general, touches the planner and
   render), or folded onto PSG3 where PSG3 is silent (songs `$81` `$86` `$8A` `$8B` have both).
   Recommended: a 4th track.
4. **`$FA` carrier TL writes:** model as voice patches the next volume change ends (as Streets of
   Rage 2's D4), or refuse and report.  Recommended: model; they block three songs.
5. **Index bound:** stop at the first repeated pointer (19 songs) or list `$81`-`$9F` with the
   duplicates.  Recommended: 19.
6. **Regression:** a few cases by coverage (the noise track, `$FA` TLs, the pitched `$85`).

Branch: `revenge_of_shinobi` (in a worktree with copied data: other sessions share this checkout).
