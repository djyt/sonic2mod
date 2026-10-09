# Streets of Rage: SMPS 68k with a MUCOM track format

Planned 2026-10-08.  Goal: `convert.py` reads Streets of Rage's songs from the ROM, as it reads
Sonic 1, Moonwalker and Golden Axe (`binary_import.md`, `docs/smps_variants.md`).

Status key: `[ ]` open, `[x]` done.  Three parts: what the driver is (1), what the code must change
first (2), the plan (3).

---

## 1. Analysis (probe 2026-10-08)

`input/roms/Bare Knuckle - Ikari no Tekken ~ Streets of Rage (World) (Rev A).md`: 512 KB,
`MK 00001019-01`, SHA-1 `731cdf18…`.  No disassembly: read with capstone (68k `$72800`-`$73C16`) and
z80dis (the Z80 player); every claim below was checked with a throwaway player against the rips
(§ 1.10).  Today `rom_import.py` stops: "no SMPS FM frequency octave".

### 1.1 What it is

SMPS 68k's engine (the "Type 1b Mucom"), with a track interpreter that reads MUCOM-style code.

| Same as Sonic 1 | Different |
|---|---|
| sound RAM `$FFF000`; queue `$FFF00A`-`C`; sound ID `$FFF009`; priority table | track bytes: duration first, then an octave/semitone note |
| IDs: music from `$81`, SFX `$A0`, `$E0`-`$E3` commands (fade, stop, ...) | flags `$F0`-`$FF`, one jump table for FM, one for PSG |
| `$30`-byte tracks: flags `+0`, channel `+1`, divider `+2` (always 1), pointer `+4`, transpose `+8`, volume `+9`, voice `+B`, stack `+C`, timeout `+E`, frequency `+10` | no tempo: a tick is a frame |
| tracks: FM1-FM5 `$FFF040`, DAC `$FFF130`, PSG `$FFF160`; SFX tracks override | loops `[ ... / ... ]n` on a per-track stack; LE offsets |
| Z80 plays the DAC from a mailbox (`$A01FFF`) | volume: a step table, absolute; header has no pitch |
| relative BE jump (`$FF`): Sonic's `code_pointer` | voices in register order, FB/ALG last |

Proposed name: `smps68k_mucom`.

### 1.2 Where things are

| What | ROM | Note |
|---|---|---|
| Driver entry | `$72914` | V-int (`$1A0A8`); PAL: a second call every 6th frame (`$10690`): 58.3 Hz |
| Pointer block | `$72800` | 9 longs: priorities `$72824`, PSG envelopes `$728D0`, music `$7288C`, SFX `$73B56`, `$728D0` x2, `$A0`, entry `$72914`, `$73C16` |
| Music index | `$7288C` | 17 longs, absolute, `$81`-`$91`; `$8D` = `$8E` |
| SFX index | `$73B56` | 48 longs `$A0`-`$CF` |
| FM table | `$732D2` | 12 words, C-B (`$284` ... `$4C0`); block = octave |
| PSG table | `$73A64` | 10 rows of 12 |
| Volume table | `$73600` | 21 TLs: `$36 $33 $30 $2D $2A $28 ... $05 $02 $00` |
| Carrier masks | `$73728` | per algorithm, TL slots `$40 $44 $48 $4C` |
| PSG envelopes | `$728E4` ... | 5 |
| Flag tables | `$73302` FM, `$73342` PSG | |
| Z80 player | `$795A2` | Kosinski, 7936 bytes; `$1061C` unpacks to `$FF7000`, copies `$1EC7` bytes |

### 1.3 Song header

```
voices.w  fm.b  psg.b                 pointers: BE, from the header
FM entries  x fm:   ptr.w  volume.b   FM1 FM2 FM3 FM4 FM5, then the DAC (FM6)
PSG entries x psg:  ptr.w  volume.b  envelope.b   tone3, tone2, tone1 (reverse chip order)
```

No tempo, no pitch.  Every song has 6 FM entries (the DAC included) and 0-3 PSG.

### 1.4 Track code

```
d n        d $01-$7F: frames; n: octave (bits 4-6) | semitone 0-11
$80|d      rest, d frames
$00        end: key off, TL $7F, track stops
$F0-$FF    flag + operands
```

Durations stop at 127; longer notes are tie chains.

| Byte | FM | PSG | DAC track | Uses FM / PSG / DAC |
|---|---|---|---|---|
| `$F0 n` | voice n | sets an unused byte | notes play sample `$80+n` | 586 / 58 / 330 |
| `$F1 v` | volume v (0-20, table) | att = (16 - v) & 15 | - | 465 / 99 / 21 |
| `$F2 lo hi m` | detune (word); m 0 sets, else adds | same, word >> 4 | - | 73 / 12 |
| `$F3 n` | gate: key off n frames before the end | same | cuts the sample | 381 / 21 / 22 |
| `$F4 0 d s lo hi c` | vibrato: delay, speed, depth word, count | same, depth >> 4 | - | 207 / 24 |
| `$F4 1`-`6 ...` | off / on / delay / speed / depth / count | same | - | |
| `$F5 lo hi` | loop start; the count at operand + offset (`$F6`'s) | same | same | 574 / 239 / 82 |
| `$F6 x n lo hi` | loop end: n passes, back to operand + 2 - offset; x unused | same | same | 574 / 240 / 82 |
| `$F7 a b c d` | FM3 special mode: fnum offsets (A2/A6, AC, AE, AD); 0 0 0 0 keeps the mode | noise (operand unused) | - | 67 / 13 |
| `$F8 n` | pan: 1 L, 2 R, else C | operand skipped | pan | 202 / 13 / 4 |
| `$F9` | pause toggle, no operand | envelope n | - | 0 |
| `$FA r v` | YM register write (carrier TL: + header volume) | envelope n (1 operand) | YM write | 213 / 3 / 7 |
| `$FB n` | volume += n steps | att -= n | skipped | 952 / 150 / 180 |
| `$FC f p a` | LFO: `$22` = f \| 8; B4 FMS p, AMS a | hangs the driver | - | 40 |
| `$FD` | tie: the next note neither keys off nor on | same | same | 377 / 108 / 1 |
| `$FE lo hi` | loop break: last pass jumps to operand + 4 + offset, clears a tie | same | same | 139 / 26 / 26 |
| `$FF hi lo` | jump, operand + 1 + signed word; clears tie and vibrato, pan RAM centred | same (pan kept) | same | 60 / 33 / 12 |

Static counts over the 17 index entries.

### 1.5 Pitch, volume, notes

- **FM pitch:** fnum = table[semitone], block = octave: real pitch, C0-B7.  96 notes; SMPS bytes
  `$81`-`$DF` hold 95, and `$90` uses C0 and B7.  No channel spans all 96.
- **PSG pitch:** row = octave; row r plays octave r + 1 (rows 0-1 clamp to row 2 = C3; rows 6-9 =
  row 6).  The rows equal Sonic's PSG table, C3-B7.  `$40` on FM = C4 = `$30` on the PSG.
- **No transposition:** the PSG adds `+8`, which nothing writes.
- **Detune:** `$F2`'s word added to the 14-bit block|fnum word (PSG: >> 4 to the divider).  Up to
  +195 (`$C3`: several semitones).
- **FM volume:** carrier TL = table[v] + header volume.  It is absolute: a voice's own carrier TLs
  never play.  `$FB` moves v in steps, so a `$FB` inside a loop crescendos.
- **PSG volume:** att = (16 - v) & 15 + header volume + envelope step, clamped to 15.  Headers
  hold `$FE`/`$FF`; an att below 0 would write a data byte (not seen).
- **Key on/off:** every note keys off first, then on.  After `$FD` it does neither: the same
  pitch rings on, a new pitch slides legato.  A `$FE` exit or `$FF` jump clears the tie.
- **Gate:** key off when n frames remain.  Skipped when the next byte is `$FD`; a note no longer
  than n plays whole.
- **Vibrato:** SMPS's shape (delay, speed, ± depth for count steps, half count first).  It tests
  before it decrements, so a half cycle is count + 1 steps.  It restarts at each untied note.

### 1.6 Voices

25 bytes: DT/MUL, TL, KS/AR, AM/D1R, D2R, D1L/RR (4 each, register order `+0 +4 +8 +C`), FB/ALG
last.  Loading a voice writes B4 from the track's pan, then the volume.  `$FA` writes (D1R, D2R,
RR, some TL) last until the next voice.  `$FA $26 $C6` (Timer B, MUCOM's tempo) is inert: nothing
reads the timer.

### 1.7 Chip features the converter has never rendered

- **FM3 special mode** (`$F7`): 7 songs (`$83 $84 $88 $89 $8C $8D $90`), FM3 track: operator 4 at
  +100 fnum.  The rips' FM3 frequency writes confirm it.
- **Hardware LFO** (`$FC`): 22 settings nonzero (e.g. freq 4 FMS 2 AMS 3; freq 7 FMS 7 = 72 Hz).
  `$22` is global: the last writer sets it for every channel.

### 1.8 PSG

- **Noise:** `$F7` on the tone3 track: white noise clocked by tone3.  In noise mode the driver
  writes no tone3 frequency, so the divider stays as last written: 0 in every rip, clocked as 1
  (Sonic's `nMaxPSG` hiss).
- **Envelopes:** 5; a step is added to att each frame from key-on.  `$81` holds, `$83` silences
  (keys off), `$80` restarts.

### 1.9 DAC

- **Z80 player:** commands `$81`-`$91` at `$1FFF`; table at Z80 `$019B`, 5 bytes each (ptr.w
  len.w rate.b).  `$81`-`$89` live in Z80 RAM; from `$8A` they read ROM bank `$78000` (voices).
- **Sample format:** a 16-byte delta table (byte 0 = run length), then nibbles, high first.  A
  nibble n adds table[n]; a 0 repeats the last delta table[0] times.  Output starts at `$80`.
- **`$85`:** length 0.  Rests and gates write it, so they **cut** the sample (Sonic's rest lets it
  play out).
- **Music samples:** `$81`-`$84`, rates 4, 1, 12, 1.  `$81` and `$82` decode byte-equal to
  Fighting in the Street's PCM bank; `$84` equals it from byte 40 on (cut by a hit).
- **Rates:** not yet measured.  The rips' DAC bytes are 2-4 samples apart at 44.1 kHz, and every
  68k YM write stalls the Z80.
- A sample from `$86` sets Z80 `$1FF6`; the music's DAC writes then wait.  It is for voice clips
  over the music.

### 1.10 Songs, rips, checks

| ID | Rip (`reference/vgz/streets_of_rage_1/`) | Frames: first pass, loop |
|---|---|---|
| `$81` | 03 Fighting in the Street | 8512, all |
| `$82` | 14 Game Over | ends 256 |
| `$83` | 01 The Street of Rage (two identical files) | ends 7080 |
| `$84` | 05 Moon Beach | 9408, 8064 |
| `$85` | 07 Beatnik on the Ship | 7424, all |
| `$86` | 09 Violent Breathing | 3136, all |
| `$87` | 12 Attack the Barbarian | 7616, 4032 |
| `$88` | 10 The Last Soul | 5814, 5760 |
| `$89` | 08 Stealthy Steps | 5120, all |
| `$8A` | none (the pack's missing 02?) | tracks loop 4608 / 2304 / 1152 |
| `$8B` | 04 Dilapidated Town | 5904, 5760 |
| `$8C` | 06 Keep the Groovin' | loop 4608 from 1536 or 2048 per track; PSG3 128 |
| `$8D` = `$8E` | 11 Level Clear | ends 469 |
| `$8F` | 15 You Became the Bad Guy! | tracks loop 2304 / 1728 / 4608 |
| `$90` | 13 Big Boss | 3584, 1792 |
| `$91` | 16 Good Ending | ends 6774 (PSG3 loops 568); the rip starts 39 frames in |

Checked with a throwaway player (tick = frame; loops, break, tie rules as above):
- **FM key-ons:** every one in the 15 rips, on its frame.  None missing; the only extras are
  log-end frames.
- **FM pitch:** fnum and block at every key-on exact, detune included.  FM3 differs only under
  special mode.
- **FM volume:** law (1.5) exact on all 5999 notes of 3 rips.
- **PSG pitch:** within 2 divider units; the player models no PSG vibrato.
- **DAC:** onsets vs the rips' seeks not reconciled (`$81`: 832 notes, 705 seeks).

### 1.11 Data quirks

- **`$89` Stealthy Steps, noise track:** `$F6` at `$7E196` closes a loop that was never opened.
  The driver decrements the next track's status byte instead, and once it wraps the stopped tone
  tracks play again: the rip has tone1 and tone2 writes to the end, where the ROM's tracks stop.
- **Unequal loop lengths:** `$8A`, `$8C`, `$8F`, `$91` loop tracks independently.
- **Duplicate:** `$8D` = `$8E`.

---

## 2. What the code must change

Below the walk, SoR shares nothing with the SMPS grammar: track bytes, headers, voices, per-kind
flags, operand pointers.  Above it, it needs driver facts the IR does not hold.  Rules applied
(`agents.md`): levels of abstraction, DRY, layering, settings first.

### 2.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. **Track grammar per variant** (`core/rom/tracks.py`).  `_decode` is SMPS's grammar.
   `decode_tracks` keeps the following, labels, layout and overlap check.  The variant supplies
   `decode(memory, address, kind)`: today's `_decode` becomes the SMPS grammar module, SoR gets
   its own.
2. **Flags per channel kind** (`SmpsVariant.flags`).  SoR's FM and PSG tables differ, and the DAC
   reads FM's with other meanings.  Make it `Mapping[ChannelType, Mapping[int, FlagSpec]]`; the
   existing variants use one table for all kinds.  Code reached from two kinds is decoded per kind.
3. **Header layout as data** (`core/rom/header.py`).  Move `_MUSIC_FIXED`, `_FM_TRACK`, `_PSG_TRACK`
   and the field offsets into `HeaderLayout`: tempo bytes or none, each entry's pointer / pitch /
   volume / mod / envelope offset, the PSG slots' chip channels.  `is_music_header` reads the same
   layout.
4. **Voice layout as data** (`core/rom/voices.py`).  Move FB/ALG first-or-last and the operator
   storage order (SMPS: `reversed`) into `VoiceLayout`.
5. **Op kinds by meaning** (`core/smps/code.py`).  The walk tells notes from durations by byte
   range, so B7 (`$E0`) cannot be a note.
   - Add `OpKind.NOTE / REST / DURATION / NO_ATTACK`; each front end (asm parser, SMPS grammar)
     decodes them.
   - Note values stay SMPS-numbered (`$81` = C0); `$E0` = B7 becomes legal.
   - Rejected: a per-channel `pitch_offset` invented to fit `$81`-`$DF`.  It is a header field
     the song does not have.
6. **The FM table check per variant** (`smps68k/locate.py`).  `locate_68k` demands Sonic's
   octave; SoR's starts at C.  Each variant states its table.  SoR's locate reuses `read_index`.
7. **One Z80 program reader** (`core/rom/z80.py`, `smps68k/dac.py`).  Today two shapes: copy
   loops, and Sonic's Kosinski `lea DACDriver`.  SoR adds a third: unpack to RAM, then copy.
8. **Row grid** (`core/plan/derive.py` `_timing`).  gcd-then-double gives SoR 1 frame (from the
   1-frame staggers), then 2, 4, 8.  Instead, choose the grid with the most onsets on rows: 7, 6,
   8, 5, 3, 13 frames here, the rest by EDx.  At BPM 150 a MOD tick is one NTSC frame, so EDx
   places the staggers exactly.  Recheck the Moonwalker and Golden Axe minimal configs.

### 2.2 New concepts

What the song must say goes in the IR; no variant check downstream (`docs/smps_variants.md`).
State is resolved in the walk where it already keeps state (the tempo divider precedent), or in
a song pass (the `run_out` precedent).

| SoR fact | IR | Resolved in | Downstream |
|---|---|---|---|
| tick = frame | divider 1, `NO_TEMPO_HOLDS` | header | none |
| FM / PSG volume steps | `PlaybackRules.volume_steps`; `VOL_STEP`, `ALTER_VOL_STEP` -> `SET_VOL`; carrier TLs read as 0 | walk | none |
| detune add | `DETUNE_ADD` -> `DETUNE` | walk | none |
| gate | `GATE` -> note + rest, marked off-grid like `run_out` | song pass beside `run_out.py` | none |
| loop break, tie cleared on exit | `OpKind.LOOP_EXIT` | walk | none |
| DAC sample by flag | `DAC_SAMPLE` -> DAC notes get the selected sample | walk | none |
| DAC rest / gate cut | `PlaybackRules.dac_rest_cuts` | rules | `channel_writer._on_rest` |
| noise leaves tone3 alone | noise notes keep the last tone note's pitch, else `nMaxPSG` (divider 0) | walk | none |
| PSG row clamp | the decoder maps to Sonic's PSG index | decoder | none |
| FM table, B7 | `fm_frequencies`, 97 entries | variant | none |
| `$FA` voice patches | `VOICE_REG` -> a patched copy of the voice (deduplicated), `SET_VOICE` swapped | song pass | none |
| FM3 special mode | `FM3_OPS` [4 offsets] -> `TrackState`; instrument key | state + catalogue | render ch3 with `$27` = `$40` |
| LFO | `LFO` [freq, AMS, FMS]; global `$22`: last writer | state + catalogue | `ym2612/voice.py` forces B4 `$C0` |
| vibrato | `MOD_SET`, depth word, count + 1 | decoder | check the vibrato formula |
| pan | `PAN` `$80` / `$40` / `$C0` | decoder | none |
| envelope `$83` | `EnvelopeCommand.MUTE`: hold att 15 | `envelopes.py` | none |
| unequal loops | check loop extension against `$8A` `$8C` `$8F` `$91` | - | maybe |

---

## 3. Plan

### Phase 0: refactors (2.1)
- [x] 0.1 Snapshot (2026-10-08, scratch, not kept): every song read (asm, 3 ROMs, fixed and
  shipped: SongCode ops and SmpsSong repr), `rom_import` listings and `--asm`, Sonic's
  `--compare`, the 35 Moonwalker / Golden Axe minimal MODs and derived configs.
- [x] 0.2 2.1 items 1-7, one commit each; each gate passed (regression, tool regression, pytest,
  ruff, pyright, vulture, the snapshot unchanged).  1 `grammar.py` + `SmpsVariant.grammar`;
  2 `flags` per `ChannelType` (`every_kind`), a header's tracks name their kind; 3 `HeaderLayout`
  `tempo` / `EntryLayout`; 4 `VoiceLayout.operator_offsets` / `feedback_last`; 5 `OpKind.NOTE` /
  `DURATION` / `NO_ATTACK` (`track_byte`); 7 `z80_ram`: copy, Kosinski, buffered Kosinski
  (SoR's, found at `$10636`), `kosinski.py` in `core/rom`.  6 not needed: SoR's index is not a
  `Go_` block, so its own locate reuses `read_index`; `locate_68k`'s octave check fails it fast.
- [x] 0.3 Row grid: where the exact grid is too fine, the multiple of it with the most notes on
  rows (was: doubled).  No Moonwalker / Golden Axe song changes (each fits at its exact grid).
  Speeds stay 2-8: a 13-frame grid (`$91`) wants speed 13 for BPM 150 exactly (Phase 4).

### Phase 1: read the ROM (done 2026-10-09)
- [x] 1.1 `SmpsDriver.MUCOM` (`smps68k_mucom`), pinned by SHA-1.  Each table found by the code
  that reads it (a lea after `subi.b #$81,d0` ...); PSG rows 2-6 checked equal to Sonic 1's.
  `HeaderLayout`: no tempo, 3- / 4-byte entries, `psg_slots` (PSG3 PSG2 PSG1).  `VoiceLayout`:
  register order, FB/ALG last.  `fm_frequencies`: 97 words (B7 included).  Envelopes: `$83`
  is `EnvelopeCommand.MUTE` (hold att 15).  The volume table waits for Phase 2.
- [x] 1.2 The grammar: notes, rests, `$00` stop, ties (`NO_ATTACK`), loops (`$F5` a body label,
  `$F6` `LOOP`, `$FE` `OpKind.LOOP_EXIT`), `$FF` jump, pan, vibrato (`MOD_SET`, count + 1),
  detune that sets, PSG noise (`PSG_FORM $E7`), drum samples (`CoordFlag.DAC_SAMPLE`, notes
  `SELECTED_SAMPLE`).  Read and dropped (reported): volume, volume step, gate, register write,
  detune that adds (Phase 2); LFO, FM3 special mode (decision 3).
  The walk: `LOOP_EXIT` (out on the loop's last pass, the tie dropped), a replay or call hands
  its tie state back (no Sonic / Moonwalker / Golden Axe song changes), DAC notes play the
  selected sample.
- [x] 1.3 `rom_import.py`: the 17 index entries read; the 48 SFX listed "not read (music only)".
- [x] 1.4 `tests/test_rom_mucom.py`: the grammar on hand-built bytes; with the ROM: tables, every
  song on its chip channels, what is dropped, envelope 3, `$89`'s loop.
- Read through the real path, every FM key-on of the 15 rips is on its frame.  Not yet: the
  jump back clears a tie (`$8F` FM1 / FM4 / FM5: the note at the loop target attacks on each
  replay, tied on the first pass).
- `$89`'s `$F6` without `$F5`: the walk unrolls a loop from its `$F6`, so it plays as written
  (16 passes) with no data fix: 6.1 is done by this.

### Phase 2: the walk
- [ ] 2.1 IR flags and walk rules of 2.2: volume steps, detune add, noise pitch, the tie the
  jump back drops.
- [ ] 2.2 Song passes: gate, voice patches.
- [ ] 2.3 Yardstick: `vgm_lift --all --configs configs/streets_of_rage` (pairs in `rips.yaml`).
  FM onsets, lengths and notes must equal the rips, as the throwaway player did.

### Phase 3: DAC
- [ ] 3.1 The Z80 program (2.1 item 7) and the sample decoder; `$81`-`$84` byte-equal to the rips'
  banks.
- [ ] 3.2 Rates: cycle-count both output paths (literal, run), then fit to the rips (Moonwalker's
  method).
- [ ] 3.3 Rests and gates cut; reconcile onsets with the rips' seeks.

### Phase 4: convert
- [ ] 4.1 `configs/streets_of_rage/`: 16 minimal configs and `rips.yaml`.
- [ ] 4.2 `vgm_pitch_audit` clean; `measure_volumes.py --configs configs/streets_of_rage`.
- [ ] 4.3 Unequal loops (`$8A` `$8C` `$8F` `$91`): loop extension or an LCM unroll.
- [ ] 4.4 Listen in the FT2 clone; Amiga merged builds if wanted.

### Phase 5: chip features
- [ ] 5.1 FM3 special mode: render FM3 with per-operator fnums.
- [ ] 5.2 LFO: render with `$22` and B4.  Loop a sample on a whole number of LFO periods.
- [ ] 5.3 Vibrato against `vgm_compare`'s vibrato rate and depth.

### Phase 6: close
- [x] 6.1 `$89`: plays as written without a fix (Phase 1).
- [ ] 6.2 `docs/smps_variants.md` section (the facts of part 1, moved); `docs/architecture.md`;
  CLAUDE.md.

### Playback rules (2026-10-09, before Phase 2)
A driver's tables, envelopes, drum names and timing are one `PlaybackRules` (`core/smps/rules.py`)
each song and channel carries; Sonic 1's are `core/drivers/reference.py` (`SONIC1_RULES`, shared
by the drivers that read none of their own).  Nothing below `core/drivers` names a driver's table:
the parser and the lift take rules, the chip renderers take a table or a divider.  Phase 2's
facts (volume steps, DAC rests that cut) go in the rules.  `CoordFlag` is a meaning (a `StrEnum`), no
driver's byte: Phase 2's new flags are names, not values parked past `$FF`.

### Test selection (the user, 2026-10-08)
Hundreds of songs cannot each be a regression case.  Phase 0 uses a one-off snapshot of
everything (scratch, not kept).  Later: cases chosen by coverage (each driver, IR flag, walk rule,
config feature exercised at least once), a song added only for what no case covers yet.

### Decisions (the user, 2026-10-08)
1. Name: `smps68k_mucom`.
2. `$89`: played as written: a `RomFix` closes the loop.
3. FM3 special mode and LFO: dropped with a report first; Phase 5 later.
4. Regression: no SoR cases.
5. Music only: the SFX are not read (1.3 lists them).

Branch: `streets_of_rage`.
