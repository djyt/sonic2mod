# Time Trax (prototype): Tim Follin's Z80 driver

Probed 2026-11-01.  Goal: `convert.py` reads Time Trax's six songs from the ROM.  Read since
2026-11-02 (Phase 1 done); this is the probe and the plan.

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

1. **A new family folder** `core/drivers/timetrax/` (D1), its `variant.py`, `names.py` / `registry.py` / `games.py` lines,
   the layer contract.  Detection by code anchors (the mailbox, the bank write, the command table).
2. **The Z80 load:** a raw copy and a banked window, as Golden Axe / OutRun.  `z80.py`'s *copied*
   shape misses it (`disasm.py --z80`: no load found): its leas sit before the bus request, not
   next to the count; widen the match.  **Big-endian words** in the bank: a `SoundMemory`
   subclass, as Mucom-MD's `memory.py` (`_BYTE_ORDER`).
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
| volume law (TL scaled) | `PlaybackRules` level law; `SetVol` from the loudest carrier, a patched voice past a threshold (D3) | walk | levels; `voice_patch.py`; `measure_volumes` | medium |
| volume mask (`$AA`) | in D3's patched voices | walk | samples | small |
| glide | `Glide(step, every)` | walk: the sounding word per frame | writer: row slides to the sounding pitch (`modulation_slides`, D4) | **large** |
| portamento | `Portamento(semitones, every)` | walk | as glide (D4) | **large** |
| FM volume envelope | `FmEnvelope(attack)` + on / off | walk | baked per (voice, envelope), as SH2's pitch envelopes (D5) | **large** |
| vibrato (2 uses) | `ModSet` approximation | grammar | `4xy` | small |

Glide, portamento and the volume law are the only parts touching most songs' sound; the rest is mapping.

### 5.3 From the `streets_of_rage_2` branch (checked 2026-11-01)

| Change | Use here |
|---|---|
| Sound IDs from 0 (R7) | song 0 is the Title |
| `core/drivers/mucom/md/` (variant, header, locate, grammar, memory) | the shape of a non-SMPS family |
| `core/rom/grammar/` package, a module per language | `grammar/timetrax.py`.  The grammar is per address, stateless: `$86` fixed duration and `$83` (no pointer back to its `$82`) need a pre-pass in the reader (MUCOM's loop end carries its offset; this one does not) |
| `VoiceChanges.levels`, `SmpsVoice.level_fixed` / `unpatched`; derive folds a patched copy onto its voice when slots run out | D3's patched voices: exact TLs out of the volume's reach; song 4's overflow folds back to plain voices.  Folding takes the least played, not the worst error |
| `core/plan/slot_fit.py`: fit 31 slots by volumes, copies, then windows | song 4 (27 voices + envelopes) |
| `TrackRules` grouped; `key_off.gate_floor` | check `$96` / `$98` on notes shorter than n against it |
| `vgm_analyze --writes` | the volume law: all four TLs per write, `$AA`'s modulators |
| `vgm_frames` FM pan check | ~1,500 pan changes a pass on song 0's FM4 |
| `vgm_lift`: FM note = table entry nearest the sounding pitch; legato slides folded | glides and the +10 cent table |
| `flag_census.py` | once the reader exists |
| `disasm.py` | 68k works now; `--z80` after the load fix |

Not useful: soft release (a rest here holds at full level), ADSR (PSG only), noise, DAC onsets, header
loops.  Still new: the volume law, glide, portamento, FM volume envelopes.

Already in main, missed in 5.2: `core/convert/vibrato.py`'s `modulation_slides` slides a row at a
time (`1xx` / `2xx`, fine `E1x` / `E2x`) to the chip's pitch at each row's end, carrying what the MOD
reached.  A glide is the same with another curve: D4 uses it.

---

## 6. Plan

### Phase 0
- [x] 0.1 Decisions § 7 (2026-11-01).
- [x] 0.2 Rips unzipped; branch `time_trax` (checked out in the main checkout, no other session).
- [x] 0.3 The rip tools on 50 Hz rips: 882-sample frames, the bursts' phase jitter (-26 to +150
  samples) inside the cut's window.  Each rip loops 9-24 frames longer than its song (6144 -> 6153,
  7440 -> 7464, 7040 -> 7052, 5376 -> 5386, 11008 -> 11023); `vgm_frames --glitches` finds a
  frame lost on every channel at once, in song 3 every ~240 frames.  Likely V-ints the driver
  drops: its main loop takes the V-int count after each update ($06F4: `ld a,(hl)` / `cp (hl)`),
  so one during a long update is never seen.  To confirm and record in `rips.yaml` (1.4).
- [x] 0.4 Tools (branch `time_trax`):
  - T1 `core/rom/z80.py`: the copied load with a bus grant wait between its leas and the count
    (`disasm.py --z80` reads it).
  - T2 `vgm_frames`: a note with a path (`PlayedNote.pitch_path` / `level_path`: what the walk
    writes after the first frame) judged on every frame it spans ("pitch path", "level path").
    Phase 2.3 / 2.4 fill the paths.
  - T3 dropped: modulator TLs are already checked, under "voice" (every register but the
    carriers' TL).
  - T4 `vgm_pitch_audit --slides [CENTS]`: a run of chip segments shorter than `--min-ms`, each
    against the MOD's `1xx` / `2xx` at its midpoint (default 50 cents).
  - T5 `disasm.py --z80 --table`: little-endian Z80 addresses.
  - Checked: `vgm_lift` on a song with no tempo holds (durations are frames); `flag_census` on
    `$80`-`$AB`.

### Phase 1: read
- [x] 1.1 `core/drivers/timetrax/` (a family of one): locate (bank, tables by code), memory
  (big-endian; `core.rom.memory.BankedMemory`, shared with SMPS Z80), header, commands, walk,
  grammar, voices.  Framework: `SmpsVariant.song_memory` (a song's tracks read through its walk),
  `core.rom.tracks.song_tracks`.  Sounds from 0; the table ends at the first header; a sound of one
  track is an SFX (not read).  `rom_import.py` lists six songs, `song_dump.py` reads each;
  `read_time_trax` snapshot.
- [x] 1.2 Every § 2.3 byte read: Pan, SetVoice, Legato, Detune as the IR has them; Phase 2's
  (transposition, volume, mask, glide, portamento, key-off timing, vibrato, envelopes) dropped by
  name ("... (not converted yet)"); the unused refused ($8F $90 $91 $9D $9E $A2-$A7); a PSG track
  refused.
- [x] 1.3 The sync: song 0's FM5 waits 636 frames each pass, read as a rest (walk.py: FM3's
  signal, FM5 reading `$8E` every other frame).  Every channel of every song loops its song's pass.
  `configs/time_trax/` (minimal, `region: pal`) and `rips.yaml`.
- [x] 1.4 Run: `vgm_lift --all --configs configs/time_trax --aspects onset length note` and
  `vgm_frames --all`: 0 of 6 clean, as expected before Phase 2.  Pitch misses a semitone (the
  transposition dropped); level the volume law; length rests that hold, `$96` / `$98`.  The rips'
  lost frames (0.3) confirmed: V-ints the driver loses ($06F4), 77 in `rips.yaml` (10-25 a song).
  Each set checked whole: every rip then loops its song's frames, and the lift's onsets fall
  (3734 / 4369 / 4798 / 2877 / 5845 -> 13 / 29 / 25 / 282 / 40; song 3's left: notes the lift
  misses at 63-66 s, Phase 2).  `vgm_analyze --updates` flags most; three it misses, placed by the
  notes: an update that ends just short of the next frame (song 0's 5618), one split by a late
  burst (song 4's 2330, 8995).  One of its flags is no loss (song 4's 3099), and song 4's 6688 sits
  somewhere in an idle 6688-6692.

### Phase 2: the walk
- [ ] 2.1 Rests as ties; `$96` / `$98`; legato; set transposition; pan; detune.
- [ ] 2.2 Volume law; `$AA` masks.  `vgm_frames` levels per key-on.
- [ ] 2.3 Glide and portamento in the walk (the frame-exact word); `vgm_frames` pitch (T2).
- [ ] 2.4 Volume envelopes; `vgm_frames` levels per frame (T2, T3).

### Phase 3: convert
- [ ] 3.1 Writer: `modulation_slides` taking any pitch curve (frame -> word), fed the walk's
  glide / portamento word (D4); envelopes baked (D5).
- [ ] 3.2 `vgm_pitch_audit` clean, slides too (T4); `measure_volumes --configs configs/time_trax`, one pass.
- [ ] 3.3 Regression cases by coverage; tool cases `read_`, `frames_`, `glitches_`.
- [ ] 3.4 Listen in the FT2 clone (the user).

### Phase 4: close
- [ ] 4.1 A section in `docs/smps_variants.md` (or a new `docs/timetrax_driver.md`: it is no SMPS);
  `docs/architecture.md`; CLAUDE.md's index; this plan to `done/`.

---

## 7. Decisions (2026-11-01)

1. **Place:** `core/drivers/timetrax/`, a family of its own (driver `timetrax`).  No claim about other
   Follin games.
2. **Region:** `pal` in the configs: the rips are 50 Hz.  An `ntsc` variant only if wanted later.
3. **Volume law:** hybrid.  `SetVol` from the loudest carrier's TL change; a patched voice per
   (voice, vol, mask) only where some operator misses by ≥ a threshold (settings.yaml; 3 TL steps =
   2.25 dB).  Measured (FM notes, one pass, envelope notes apart):

   | Song | Voices | Exact (voice, vol, mask) | Hybrid, ≥ 3 | Notes ≥ 3 off under `SetVol` |
   |---|---|---|---|---|
   | 0 | 17 | 17 | 17 | 0 |
   | 1 | 19 | 29 | 23 | 114 / 2888 |
   | 2 | 14 | 20 | 19 | 122 / 3796 |
   | 3 | 16 | 17 | 16 | 0 |
   | 4 | 19 | 33 | 27 | 366 / 4231 |
   | 5 | 9 | 9 | 9 | 0 |

   Song 4 then has 4 slots left for envelopes and detune variants: expect banks or a lower threshold
   trade there.
4. **Glides and portamento:** row slides, `core/convert/vibrato.py`'s `modulation_slides`
   generalised from a `ModSet` to any curve: each row `1xx` / `2xx` (fine `E1x` / `E2x`) to the
   sounding pitch at its end, the error carried.  The curve is the walk's frame-exact word, glide
   on top of portamento; portamento's semitone steps smooth within a row.
5. **Volume envelopes:** baked per (voice, envelope), as Space Harrier II's pitch envelopes.
6. **Tuning:** as heard on PAL: +10 cents, the table's words carried.
7. **SFX:** not converted.
