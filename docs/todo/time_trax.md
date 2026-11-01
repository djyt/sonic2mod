# Time Trax (prototype): Tim Follin's Z80 driver

Probed 2026-11-01.  Goal: `convert.py` reads Time Trax's six songs from the ROM.  Nothing implemented;
this is the probe and the plan.

**Verdict.**  Not SMPS: Tim Follin's own Z80 driver, a new family.  *Reading* it is small: a 100-line
scratch walker decodes all six songs, and their key-ons land on the rips' frames with the rips'
frequency words.  *Converting* it is the bulk: Follin's style lives in effects the IR lacks: fnum
glides and semitone portamento (2,494 commands a pass, in every song), a multiplicative volume law,
FM volume envelopes, rests that hold.  Unused by the music: PSG, DAC, LFO, random branches.

Status key: `[ ]` open, `[x]` done.  Parts: the ROM and rips (1), the driver (2), what it plays (3),
the prototype (4), what the code must change (5), the plan (6).  Decisions: § 7.

---

## 1. ROM and rips

| File | Size | SHA-1 |
|---|---|---|
| `input/roms/Time Trax (USA) (Proto).md` | 1 MB | `d7eb65f67ebb89a85c25a3accf06368554a13834` |

Header is a placeholder: `SEGA LOGO AND HARDWARE DEMONSTRATION`, `(C)SEGA 1993.03`, serial
`AI 0000000 -00`.  Developer Malibu Interactive, music Tim Follin, unreleased.  No other ROM in
`input/roms/` holds the driver (its FM table and note countdown searched in all 13).

Rips: VGMRips 1.02 (RichterEX2), `reference/vgz/time_trax/`, **still zipped**: unzip beside it.
- **Recorded at 50 Hz** (header rate 50, frames 882 samples apart), FM clock in the header NTSC's.
- YM2612 only.  PSG: one write per register at the start, no notes.  DAC: `$2B` = 0 for the whole rip;
  the Z80's idle loop streams `$80` to `$2A` (80k-1.3M inaudible writes a rip, data blocks of 24-536 bytes).

---

## 2. The driver (probe: capstone 68k, z80dis Z80, scratch)

### 2.1 Where things are

| What | Where | Note |
|---|---|---|
| Z80 program | ROM `$C618E`, `$1A8E` bytes | raw copy to Z80 `$0000` (68k `$C6126`: `lea $A00000` / `dbra`) |
| 68k mailbox | Z80 `$00E0` | 16 × (arg, command); write index `$0009`, read `$0008` (68k `$C60DE`) |
| Commands | Z80 `$073A` | 0 play song arg, 1 stop all, 2 skip to the next event, 3 test track, 4 delay, 5 master volume = arg, 6 pause / resume |
| Update | V-int (`RST $38` counts `$06D3`) | one update a frame; no tempo: **a duration is frames** |
| Music bank | ROM `$F8000`-`$FFFFF` | Z80 window `$8000`; bank set by `$0C96` (`$6000` serial writes) |
| Bank header | `$8000` | `$5D`; then **big-endian** pointers: `$8001` song table, `$8003` voices, `$8005` volume envelopes |
| Song table | `$8007` | 39 entries: 0-5 music, 6-38 SFX |
| Voices | `$8205` | 97 pointers; entry 0 junk (`$0040`), songs use 1-65 |
| Volume envelopes | `$82C7` | 28 pointers |
| DAC bank | ROM `$F0000` | 4-byte entries (addr.w, length.w), streamed 128 bytes at a time through Z80 `$0200` |
| FM table | Z80 `$1804` | 96 words, block in bits 11-13: 8 octaves of 12 from B |
| PSG table | Z80 `$18C4` | dividers from 1017 (A2) down |
| Track RAM | Z80 `$0300` | 8 × `$7A` bytes: FM1-FM6, PSG tone 2, PSG tone 3 / noise |
| Command tables | Z80 `$0DC8` FM, `$0E20` PSG | 44 words each, `$80`-`$AB` |

Self-modifying: `RST $10` writes to `$4000 + ($0012)` (port 0 or 2); `$0B0E` is patched `RET` / `LD A,E`
to stop or run the DAC refill.

### 2.2 Song header

```
entries: prio.b  channel.b  ptr.w (big-endian)  ... $FF
```
Channel 0-5 = FM1-FM6, 6-7 = the two PSG tracks.  Music: priority 0, five or six FM channels, no PSG.

| ID | Channels | Loop (frames) | 50 Hz / 60 Hz | Rip |
|---|---|---|---|---|
| 0 | FM1-6 | 6144 | 2:03 / 1:42 | 01 Title Screen, Credits |
| 1 | FM1-5 | 7440 | 2:29 / 2:04 | 04 Stages 2, 5, 7 |
| 2 | FM1-5 | 7040 | 2:21 / 1:57 | 05 Stages 4, 6 |
| 3 | FM1-5 | 5376 | 1:48 / 1:30 | 03 Stages 1, 3, 8 |
| 4 | FM1-5 | 11008 | 3:40 / 3:03 | 02 Mission Briefing |
| 5 | FM1-6 | ends (`$80`) | 0:13 | 06 Game Over |

The rips' loop lengths are the 50 Hz column.

### 2.3 Track code

`$01`-`$7F` a note, `$00` a rest, each followed by a duration byte (frames, 1-255) unless fixed
duration (`$86`) is on.  `$80`-`$AB` a command.  Note n plays table[n + transposition].

| Byte | Args | FM | Used in music (one pass) |
|---|---|---|---|
| `$80` | | stop | 6 |
| `$81 w` | 2 | jump (absolute, big-endian) | 32 |
| `$82 n` / `$83` | 1 / 0 | loop n times / loop end (3-byte stack entries) | 679 |
| `$84 w` / `$85` | 2 / 0 | call / return | 2911 |
| `$86 n` | 1 | fixed duration n (0: off): notes carry no duration byte | 55 |
| `$87 n` | 1 | pan: `$FF` L, 0 C, 1 R | 2568 |
| `$88 n` | 1 | voice n | 4394 |
| `$89 n` | 1 | transposition **set** (signed) | 174 |
| `$8A v` | 1 | volume v (0-255); stops an envelope | 3759 |
| `$8B` / `$8C` | 0 | notes re-key / legato (no key-off / key-on) | 2627 / 2857 |
| `$8D n` / `$8E` | 1 / 0 | signal channel n / wait for a signal | 1 / 1 |
| `$8F w` / `$90` | 2 / 0 | jump unless signalled / loop end unless signalled | 0 |
| `$91 n w×n` | 1 + 2n | jump to a **random** one of n | 0 |
| `$92 s f` / `$93` | 2 / 0 | glide: s fnum units every f frames toward the note / off | 1341 |
| `$94 s f` / `$95` | 2 / 0 | portamento: s semitones every f frames toward the note / off | 1153 |
| `$96 n` / `$97` | 1 / 0 | key off n frames before the end / off | 401 |
| `$98 n` / `$99` | 1 / 0 | key off n frames after the start / off | 1785 |
| `$9A` 6 bytes / `$9B` / `$9C` | | vibrato (delay, depth step, waveform, ...) / restart / off | 2 |
| `$9D` / `$9E` | 0 | portamento ignores / follows transposition | 0 |
| `$9F a r` / `$A0` / `$A1` | 2 / 0 / 0 | volume envelopes: attack a at each note, release r / on / off | 87 / 88 / 349 |
| `$A2 n` / `$A4 n` (+ offs) | 1 | release envelope n frames before end / after start | 0 |
| `$A6 n` | 1 | LFO (`$22` = n-1 \| 8; 0 off) | 0 |
| `$A7 n` | 1 | DAC sample n (`$2B` = `$80`, refill on) | SFX only |
| `$A8 d` / `$A9` | 1 / 0 | detune: d (signed) added to the frequency word / off | 29 |
| `$AA m` / `$AB` | 1 / 0 | operators the volume scales: mask m / the algorithm's carriers | 24 / 2 |

PSG table: the same but `$87 n` noise mode (0 off, 1 periodic, 2+ white, at tone 3's rate), `$88`
noise off, `$96`-`$99` null (jump to `$0000`: reset).

**Voice:** 26 bytes, register order (OP1 OP3 OP2 OP4): `$B0`, `$B4` (AMS/FMS, pan ORed in), 4 × `$30`,
4 × `$40` (TL, written through the volume law), 4 × `$50`, `$60`, `$70`, `$80`.  No SSG-EG.
Carrier masks per algorithm at Z80 `$006A`: `01 01 01 01 03 07 07 0F`.

**Volume envelope:** start level, then (frames, level) segments ramped linearly; `$FE` loops,
`$FF` holds.  E.g. envelope 0: 230, to 200 over 192 frames, to 245 over 64.

---

## 3. How it plays

- **Note:** key-off then key-on, unless legato.  **A rest writes nothing: the note sounds on**
  (only `$96` / `$98` or the next note key it off).
- **Volume law** (`$1343`): per operator, `TL' = 127 - (127 - TL) × master/256 × vol/256`
  (quarter-square multiply, table at Z80 `$0100`).  Master (`$000A`, `$FF`) scales the algorithm's
  carriers, vol the operators in the mask (`$AA`, default the carriers).  Multiplicative in TL, not
  added: vol 200 takes a carrier at TL 10 to TL 36 (−19 dB), at TL 40 to TL 59 (−14 dB).
- **Glide** (`$1739`): the sounding word moves s fnum units every f frames from where it was toward
  the note's word, crossing blocks at fnum `$4D2` / `$245`.  Songs use f = 1, s = 2-250.
- **Portamento** (`$16C8`): the note index steps s semitones every f frames from the previous note;
  songs use (1,1) (3,1) (4,1) (5,1) (1,2).  Glide runs on top of it.
- **FM table:** A = fnum `$44B` (1099) vs 1083 at A440: **+25 cents** on the NTSC clock, +10 on PAL's.
  Played as heard.
- **Sync:** song 0's FM3 signals FM5 (`$8D 5`, `$A01B`) just before its loop's jump; FM5 waits
  (`$8E`, `$A2B8`) near the end of each pass: its 5508-frame pass stretched to FM3's 6144.
- **Rate:** V-int.  The rips run at 50 Hz; a 60 Hz console plays everything 20 % faster.  MOD BPM 125
  (50 Hz) and 150 (60 Hz) both give one tick a frame.
- **Not by the music:** PSG tracks, DAC, LFO, random jumps, release envelopes, `$8F` / `$90`.

---

## 4. The prototype (scratch, not kept)

A walker (`ttwalk.py`, `ttcheck.py` in the session scratchpad) with § 2.3's argument counts:
- All six songs decode; no unknown byte; every channel's loop length agrees within its song
  (song 0's FM5 short by the wait's 636 frames).
- Key-ons (legato skipped) against the rips, first 6-12 per channel: **every frame and frequency
  word matches** after a constant lead-in (17-41 frames a rip), but where a glide was mid-way
  (`$1AB4` vs `$1A8F`) or detune applied.  FM5 lands a frame late in song 3 (to check: channel order
  or a rip glitch).

| ID | Notes (one pass, ≈) | Voice changes | Glide / portamento |
|---|---|---|---|
| 0 | 4387 | 687 | 58 / 169 |
| 1 | 2926 | 647 | 223 / 144 |
| 2 | 3833 | 1188 | 384 / 298 |
| 3 | 2885 | 562 | 381 / 191 |
| 4 | 4881 | 1280 | 273 / 339 |
| 5 | 227 | 30 | 22 / 12 |

---

## 5. What the code must change

Rules: `agents.md`; `docs/smps_variants.md` § Adding a variant; nothing outside the driver's folder
names it.

### 5.1 Reading (small)

1. **A new family folder** (D1), its `variant.py`, `names.py` / `registry.py` / `games.py` lines,
   the layer contract.  Detection by code anchors (the mailbox, the bank write, the command table).
2. **The Z80 load:** a raw copy (`core/rom/z80.py`'s uncompressed copy) and a banked window, as
   Golden Axe / OutRun; **big-endian words** in the bank (`SoundMemory.word` takes byte order).
3. **The grammar:** § 2.3 through the shared code walk (`core/smps/code.py`): a note and its
   duration, fixed duration as grammar state, a call stack, counted loops.
4. **The header:** (prio, channel, ptr) lists; voices (26 bytes, register order) through
   `VoiceLayout`; the FM table into `PlaybackRules.fm_frequencies` (its cents carried, as Space
   Harrier II's words).
5. **The sync:** resolved in the reader: walk FM3 to its signal's frame (durations are frames), turn
   FM5's wait into a rest that long.  No IR change.

### 5.2 New concepts (the bulk)

| Fact | IR | Resolved in | Downstream | Size |
|---|---|---|---|---|
| a rest holds the note | the grammar emits a tie (`NO_ATTACK`), not a rest | grammar | none; check the gates against ties (`gate_spares_tied`) | small |
| `$98` key-off after n frames | `NOTE_FILL` (Sonic 1's: frames) | grammar | none | none |
| `$96` key-off n before the end | `Gate` (Streets of Rage's) | grammar | none | none |
| legato on / off | `Legato` (Space Harrier II's) | walk | none | none |
| transposition **set** | `SetTransposition` | walk | none | small |
| pan `$FF` / 0 / 1 | `Pan` | grammar | `8xx` per note (check the writer emits `Pan`) | small |
| detune | `Detune` (raw fnum) | grammar | detune variants | none |
| volume law (TL scaled) | `PlaybackRules` level law; `SetVol` from the loudest carrier (D3) | walk | levels; `measure_volumes` | medium |
| volume mask (`$AA`) | a patched voice per (voice, vol) | walk (`voice_patch.py`) | samples | small |
| glide | `Glide(step, every)` | walk: the sounding word per frame | writer: `3xx` at a speed (the writer has only `3FF`) (D4) | **large** |
| portamento | `Portamento(semitones, every)` | walk | `3xx`, or a note per step (D4) | **large** |
| FM volume envelope | `FmEnvelope(attack)` + on / off | walk | baked per (voice, envelope), as SH2's pitch envelopes (D5) | **large** |
| vibrato (2 uses) | `ModSet` approximation | grammar | `4xy` | small |

Glide, portamento and the volume law are the only parts touching most songs' sound; the rest is mapping.

---

## 6. Plan

### Phase 0
- [ ] 0.1 Decisions § 7.  Unzip the rips; branch `time_trax` in a worktree (other sessions share this
  checkout).
- [ ] 0.2 Check the rip tools on 50 Hz rips (`vgm_frames` / `vgm_lift` frame detection; `region: pal`).

### Phase 1: read
- [ ] 1.1 5.1 items 1-4; `rom_import.py` lists six songs; `song_dump.py` reads, walks and plays each;
  `read_time_trax` snapshot.
- [ ] 1.2 Unknown-effect policy: the grammar maps every § 2.3 byte; unused ones (`$91`, `$A6`, `$A7`,
  PSG) refused with a message.
- [ ] 1.3 The sync (5.1.5).  `configs/time_trax/rips.yaml`, minimal configs, `region` (D2).
- [ ] 1.4 `vgm_lift --aspects onset length note`; `vgm_frames` on all six.

### Phase 2: the walk
- [ ] 2.1 Rests as ties; `$96` / `$98`; legato; set transposition; pan; detune.
- [ ] 2.2 Volume law; `$AA` masks.  `vgm_frames` levels per key-on.
- [ ] 2.3 Glide and portamento in the walk (the frame-exact word); `vgm_frames` pitch.
- [ ] 2.4 Volume envelopes.

### Phase 3: convert
- [ ] 3.1 Writer: speed `3xx` for glides / portamento (D4); envelopes baked (D5).
- [ ] 3.2 `vgm_pitch_audit` clean; `measure_volumes --configs configs/time_trax`, one pass.
- [ ] 3.3 Regression cases by coverage; tool cases `read_`, `frames_`, `glitches_`.
- [ ] 3.4 Listen in the FT2 clone (the user).

### Phase 4: close
- [ ] 4.1 A section in `docs/smps_variants.md` (or a new `docs/follin_driver.md`: it is no SMPS);
  `docs/architecture.md`; CLAUDE.md's index; this plan to `done/`.

---

## 7. Decisions (open, for the user)

1. **Name and place.**  Proposed: a family `core/drivers/follin/` with `timetrax/` in it (driver
   `follin_z80`).  The family's name says it is not SMPS; a second Follin game would sit beside it.
2. **Region.**  The rips are 50 Hz; a US cartridge would play 20 % faster.  Recommended: `pal` in the
   configs (matches the rips; every tool checks against them), a `ntsc` variant if wanted.
3. **Volume law.**  `SetVol` from the loudest carrier's TL change (fits baked levels; exact for one
   carrier, small timbre error for several), or a patched voice per (voice, vol) (exact, more samples).
   Recommended: the first.
4. **Glides and portamento in the MOD.**  `3xx` at a speed per note (FT2 slides periods, the chip
   fnum: approximate), or bake each glide into a sample (exact, a sample per (voice, from, to)).
   Recommended: `3xx`; portamento's 1-frame semitone steps as the same `3xx`.
5. **Volume envelopes.**  Baked per (voice, envelope) (as Space Harrier II's pitch envelopes), or
   `Axy` / `Cxx` per row.  Recommended: baked.
6. **SFX** (33 entries, PSG and DAC ones among them): not converted, as for every other driver.
