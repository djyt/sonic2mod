# Space Harrier II: an early SMPS Z80

Planned 2026-10-10.  Goal: `convert.py` reads Space Harrier II's songs from the ROM, as it reads
Sonic 1, Moonwalker, Golden Axe and Streets of Rage (`docs/smps_variants.md`).  Branch
`space_harrier_2`.

Status key: `[ ]` open, `[x]` done.  Three parts: what the driver is (1), what the code must change
(2), the plan (3).

---

## 1. Analysis (probe 2026-10-10)

`GM 00004002-01` (World), SHA-1 `db4285e4…`.  No disassembly: read from its Z80 driver with
z80dis.  20 rips: `reference/vgz/space_harrier_2/`.  Moves to `docs/smps_variants.md` when done.

**Family.**  Golden Axe's ancestor: same note grammar, `$EF` voice, `$F0` set volume, `$F2` stop,
`$F6`-`$F9`, `$FB`, `$FC` slide mode, `$FD` raw frequency, a drum track on FM3.  Little shared code
(117 bytes).  Its register / value voice loader (`cp $83 / ret z / cp $B0`) is also in Super Thunder
Blade, Zoom!, Alex Kidd in the Enchanted Castle, Altered Beast, Super Hang-On and Rambo III; the
flag dispatch matches byte for byte only in Alex Kidd's.

**Where things are**
- **Programs:** the 68k loads one of three to Z80 `$0000` (verified copy, `core/rom/z80.py`): the
  driver (ROM `$16A6C`, `$14F6` bytes) and two PCM voice players (`$14724`, `$15904`).
- **Bank:** ROM `$10000` at Z80 `$8000`, set by 9 writes to `$6000`; pointers little-endian.  The
  PSG is written through the bank window (`$C00011`).
- **Tables (bank):** music index `$87FC` (songs `$81`-`$99`), tempo `$87E3`, priorities `$87A4`,
  pitch envelopes `$863B`, voices `$883A`.  **(driver):** FM `$098F`, PSG `$08FD`, flag jumps `$05B7`,
  SFX index `$14BF` (`$A0`-`$B8`).
- **Queue:** `$1804`-`$1806`.  Track RAM `$1839`: 11 tracks of `$30` (7 music, 4 SFX).

**Songs.**  No SMPS header: a count byte, then 9-byte track records copied into track RAM (flags,
channel, divider, pointer, transposition, pitch envelope, voice / envelope, volume).  Every song:
FM1-FM6 and PSG3; FM3's and PSG3's records share the drum track's pointer.  No PSG tone music.

**How it plays**
- **Durations:** byte x divider.  **Tempo:** every n frames ($1802) each music track holds a frame;
  0 never holds (most songs; 3 in `$8C`, `$93`, `$95`, `$99`).
- **Flags:**

  | byte | Space Harrier II | music uses |
  |------|------------------|------------|
  | `$E0`-`$E6`, `$F3` | PSG noise form (operand ORed with `$E0`) | drums |
  | `$E7` | LFO (`$22`) | - |
  | `$E8` | a driver byte (`$1808`: stops the music when set) | - |
  | `$E9` | the song the 68k queues next (`$1809`) | 40 |
  | `$EA` `$EB` `$FF` | FMS, AMS, pan (`$B4`) | 52 (`$FF`) |
  | `$EC` `$ED` | FM3 special mode on / off, no operand | drums |
  | `$EE` | legato on (1) / off: no key-off between notes | 96 |
  | `$EF` | voice | 618 |
  | `$F0` `$F1` | set volume | 8 |
  | `$F4` | pitch envelope | 12 |
  | `$F5` | PSG envelope (PSG tracks) | drums |
  | `$FA` | divider | drums |
  | `$FB` | transposition (add) | 60 |
  | `$FC` `$FD` | slide mode, raw frequency mode | drums |
  | `$FE` | alter volume | 68 |

  Track flag bit 6 (record byte 0 `$C0`): pan animation (table Z80 `$0482`).
- **Voices:** (register, value) pairs, channel-0 registers, `$83` ends.  Carriers take the volume.
- **Pitch:** its own FM table, block in bits 11-13 (fnum `$269`-`$48C`, ~30 cents sharp of
  Sonic 1's).
- **Pitch envelopes:** per-frame fnum offsets; `$80` restart, `$84 n` multiplier += n, `$85 n` jump
  to step n, `$81`-`$83` hold.  Envelopes 1-3 scoop, then loop a vibrato whose depth grows each pass.
  49 tracks start with one.
- **Drums:** the drum byte is a bitmask.  FM: two units on FM3 in special mode, each an operator
  pair with its own frequency sweep (records Z80 `$0BE6`-`$0C18`; bits 3 / 0 one unit, bit 2 the
  other, bit 5 a variant).  PSG: a tone 3 sweep and noise 7 (`$0E61`, bits 0-3).  The rips' frame
  logs show both.

---

## 2. What the code must change

### 2.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. [x] **Z80 programs kept apart** (`ab2e876`): `z80_loads`, the verified copy; Kosinski refuses
   bytes that are no stream (Super Thunder Blade crashed the reader).
2. [x] **The driver among them** (`ab2e876`): `smpsz80/program.py` `driver_ram`, `fm_table`
   (from Type 0 FM's `locate.py`).
3. [x] **Header readers per variant** (`c18eb37`): `SmpsVariant.music_header_reader`,
   `sfx_header_reader`.
4. [x] **Voice reader per variant** (`c18eb37`): `SmpsVariant.voice_reader`.
5. [ ] **Drum model** (`core/smps/percussion.py`, `core/synth/fm_render.py`, `fm_samples.py`,
   `core/convert/fm_drums.py`, `core/plan/instruments.py`).  `FmDrum` is one voice, one frequency
   word a frame, FM only.  Generalise: a frame carries a word per operator (special mode:
   `fm_render._set_special_freq` exists), and an optional PSG part (a divider, noise mode and
   attenuation a frame: `psg_render` has no frame-by-frame renderer yet), mixed into the one-shot.
   Golden Axe's drums render byte-identical.

### 2.2 New concepts

| Fact | IR | Resolved in | Downstream |
|---|---|---|---|
| track-list header, tempo table | `SmpsSongHeader`; the drum track once (FM3 + PSG3 one DAC channel) | header reader | none |
| tempo hold every n frames | `tempo_modifier` as Sonic 1's TempoWait (check the phase) | header | none |
| register / value voices | `SmpsVoice` | voice reader | none |
| legato `$EE` | persistent tie: `NO_ATTACK` before each note while on | walk | none |
| alter / set volume | `ALTER_VOL`, `SET_VOL` | decoder | none |
| pan, AMS / FMS, LFO | `PAN`, `LFO` (Streets of Rage's) | decoder | none |
| pitch envelope | new: `PitchEnvelope` effect, the table in `PlaybackRules` | walk | channel_writer (decision D1) |
| pan animation | decision D2 | - | - |
| follow-on song `$E9` | decision D3 | - | - |
| FM / PSG drums | the drum model (2.1.5) | drum reader | render |

---

## 3. Plan

### Phase 0: refactors
- [x] 0.1 2.1 items 1-4; gates passed (regression, tool regression, pytest, ruff, pyright,
  vulture, layers).
- [x] 0.2 Merge main (`c254462`: chips in `core/chips/`, renderers in `core/synth/`); gates passed.

### Phase 1: read the ROM
- [ ] 1.1 `SmpsDriver` name (`smpsz80_sh2`? the family's name once a second game reads), folder
  `core/drivers/smpsz80/<name>/`, pinned by SHA-1 in `games.py`.
- [ ] 1.2 Locate by shape: the bank from the 9 writes, each table from the `ld hl,nn` that reads it;
  `SoundIndex`, music `$81`-`$99`.  SFX: listed, not read (music first, decision D4).
- [ ] 1.3 Header reader: the track records -> `SmpsSongHeader` (chip channel from the channel
  byte; FM3 + PSG3 sharing a pointer: one drum track); tempo from the table.  Confirm record byte 7
  (FM: overwritten by the first `$EF`?).
- [ ] 1.4 Flags (1 above), voices (register / value), FM and PSG tables (`rules_from_rom`).
- [ ] 1.5 `rom_import.py` lists the songs; `tools/song_dump.py` reads every one; read snapshot
  `read_space_harrier_2`.
- [ ] 1.6 Tests: `tests/core/drivers/smpsz80/<name>/` (hand-built bytes; with the ROM: tables,
  tracks, voices).

### Phase 2: the walk
- [ ] 2.1 Legato `$EE`, volume, transposition, divider, pan / AMS / FMS, LFO.
- [ ] 2.2 Tempo holds: the phase against the rips' key-on frames (Golden Axe's was a frame late).
- [ ] 2.3 Pitch envelopes (D1): the IR effect, the walk, the converter.
- [ ] 2.4 Yardstick: `configs/space_harrier_2/` minimal configs, `rips.yaml` (20 rips to `$81`-`$99`);
  `vgm_lift`, `vgm_frames` (pitch, level, voice at every key-on).

### Phase 3: drums
- [ ] 3.1 Refactor 2.1.5 (Golden Axe byte-identical).
- [ ] 3.2 The drum reader: each drum byte's FM units and PSG part, frame by frame, from the
  records and the selection code (`$0CC0`, `$0DFE`; read it exactly: `$0D2C` loads `$0BF0` either way).
- [ ] 3.3 Against the rips' FM3 / PSG3 / noise frames: every hit.

### Phase 4: convert and verify
- [ ] 4.1 Every song converts; `vgm_pitch_audit`, `vgm_compare`, `measure_volumes --configs
  configs/space_harrier_2`.
- [ ] 4.2 Cases in `tests/cases.yaml` (chosen by coverage), `frames_space_harrier_2` in
  tool_regression.
- [ ] 4.3 `docs/smps_variants.md` § Space Harrier II (section 1 moves there); CLAUDE.md index.

### Decisions (the user's)
- **D1 Pitch envelopes:** MOD effects (`1xx` / `2xx` / `4xy` a row, as vibrato is now) or baked
  into each note's sample (exact, more samples).
- **D2 Pan animation:** drop, or `8xx` per row.
- **D3 `$E9` follow-on:** each song alone, or a song's MOD runs on into the one it queues.
- **D4 SFX:** music only (as Streets of Rage), or SFX through `sonic2wav.py` too.
- **D5 The family:** after Space Harrier II, which game next (a plan each); most load their
  driver in shapes `z80.py` does not know yet (Altered Beast: an unrolled copy).
