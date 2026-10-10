# SMPS variants

The SMPS drivers the ROM reader knows (`core/drivers/`), what each differs from Sonic 1 by, and how
each fact was found.  Sonic 1 itself is `docs/smps_driver.md`; how the reader is built is
`docs/architecture.md` § 3; the plan and its history `docs/todo/binary_import.md`.

| Variant (`driver:`) | Family | Game | Code |
|---|---|---|---|
| `sonic1` (Type 1b, modified) | SMPS 68k | Sonic the Hedgehog | `core/drivers/smps68k/sonic1/` |
| `smps68k_type1a` | SMPS 68k | Michael Jackson's Moonwalker | `core/drivers/smps68k/type1a/` |
| `smpsz80_type0fm` (an early Type 1 FM) | SMPS Z80 | Golden Axe | `core/drivers/smpsz80/type0fm/` |
| `smps68k_mucom` (Type 1b, MUCOM-style track code) | SMPS 68k | Streets of Rage | `core/drivers/smps68k/mucom/` |

A ROM known by its SHA-1 is pinned to its variant; any other is tried with each (`detect.py`).

---

## SMPS 68k Type 1a: Michael Jackson's Moonwalker

`GM 00004028-01` (Rev A), SHA-1 `70d9b760…`.  No disassembly: read from its driver with capstone
(`$60000`-`$61700`) and `reference/sega_retro/`.

- **Sounds:** music index `$600A4`, 23 songs `$81`-`$97` (Sega Retro's Rev 01 column), in the
  driver's `Go_` block at `$60000` (Sonic 1's order: priorities `$60100`, special SFX `$67C78` (4),
  music, SFX `$67BC4` (49), speed-up, PSG_Index `$60020` (6 envelopes)).
- **As Sonic 1:** song headers (relative pointers, DAC first, 6-byte PSG entries), 25-byte voices
  in the same register order, TempoWait (`$60BC0`), durations x divider, PSG pitch (`note - $81 +
  transpose`, 69-entry table `$611F6`), FM pitch (a one-octave table at `$61024`, block = `(note -
  $80 + transpose) / 12`: Sonic 1's 96 words per block; only notes past the top wrap differently),
  modulation (`$F0`), note fill, jump / loop / call.
- **Flags** (jump table `$61290`, 31 entries; `$FF` runs into `$E0`'s handler):

  | byte | Type 1a | Sonic 1 | used |
  |------|---------|---------|------|
  | `$E3` | sets a global flag (`$FC` clears it), 0 operands | return | no |
  | `$E4` | pan animation: mode, 0 → off, else table, index, limit, speed | fade | 6 |
  | `$E5` | alter volume: FM byte, PSG byte | channel tempo divider | no |
  | `$E9` | LFO: register `$22`, AMS/FMS | transposition | no |
  | `$EA` `$ED` `$EE` | detune (as `$E1`) | tempo / push / stop special | no |
  | `$EB` | queue a sound (`$DD`, a voice sample) | tempo divider, all tracks | 5 |
  | `$F9` | return | FM1 release rate | 75 |
  | `$FA` | channel tempo divider | - | 4 |
  | `$FB` | transposition | - | 63 |
  | `$FC` | clears `$E3`'s flag | - | no |
  | `$FD` | SSG-EG, 4 operands | - | no |
  | `$FE` | FM3 special mode, 8 operands | - | no |

- **Carrier TL = the track volume** (not Sonic 1's voice TL + volume): loading a voice (`$61402`)
  writes its 24 operator registers, then `$61478` writes each carrier's TL (Sonic 1's slot mask,
  `$614C0`) from `$9(a3)` alone, as `$E6` does after changing it.  The voice's own carrier TLs never
  play (`VoiceLayout.carrier_tl`); a volume with bit 7 set writes none.  Found by
  `tools/vgm_frames.py`: an algorithm 5 voice's third carrier 27 TL quieter than every rip.
- **PSG envelopes:** the ROM's six, ending `$83` (hold); `$80` restarts, `$85 nn` jumps to step nn;
  envelope 6 has no terminator and runs into 5.
- **DAC:** a Z80 sample player copied uncompressed (code `$6166A`, samples `$61876`, to Z80
  `$0210`): 12-byte table entries, 5 samples in Z80 RAM (from `$86` they stream from ROM banks:
  the voice samples, not read).  The 68k remaps notes: `$88`-`$8F` play `$85` at pitches from
  `$602FA`, `$90`+ play `$82` at a patched rate.  Rates fitted to the rips: 235.6 + 13.96 (pitch -
  1) cycles a byte.
- **Tempos:** modifiers 3-32 and 255 (the title jingle).

Against the rips (`tools/vgm_lift.py`): every song's FM plays as recorded, but FM5's first note,
a tick late in 4 rips.  Against their frame logs (`tools/vgm_frames.py`): every FM note's
pitch, level and voice as recorded, but that note and the logs' loop and end frames.

---

## SMPS Z80 Type 0 FM: Golden Axe

`GM 00054018-01` (Rev A), SHA-1 `2ce17105…`.  An early, simpler Type 1 FM (Sonic Retro's class for
the FM-drum Z80 games: Flicky, Fighting Masters).  No disassembly: read from its Z80 driver with
`z80dis` (`pip install z80dis`; capstone has no Z80).

**Where things are**
- **Driver:** copied uncompressed by the 68k (`$34DC`: `$DF8` bytes, ROM `$1D2F0` -> Z80 `$0000`;
  `core/rom/z80.py` reads the copy loop).  Found by its FM table (`type0fm/locate.py`).
- **Bank:** set once by the 68k (`$1C00` = 1, `$1C01` = `$80`): Z80 `$8000`-`$FFFF` = ROM
  `$18000`-`$1FFFF`.  Every song, voice and pointer lives there; pointers are absolute Z80
  addresses, little-endian.  Found as the one bank starting with a sound header.
- **Sound header** (`$8000`, words): `+0` priorities, `+4` music index (`$8069`: 15 songs
  `$81`-`$8F`), `+6` SFX index (`$8087`: `$90`-`$B9`), `+8` pitch envelopes (no music track uses
  one).  Queue commands `$E0`-`$E3`: fade, stop, ?, the SEGA voice (DAC, Z80 `$0F00`).
- **Songs:** Sonic 1's header fields; every song 6 FM, 0 PSG.  `$8A`, `$8F` are stubs.  SFX: Sonic
  1's layout, tracks on FM3-FM6 and the PSG, one shared voice bank at `$80DB`.

**How it plays** (`type0fm/layout.py`, `type0fm/variant.py`)
- **Track order** (Z80 `$0501`): the drum track, FM1, FM2, FM4, FM5, FM6.
- **Flags** (Z80 `$0B65`): a handler starts at the first operand and the driver steps one byte
  past it, so a flag with no handler skips one operand.

  | byte | Type 0 FM | Sonic 1 | music uses |
  |------|-----------|---------|------------|
  | `$E0`-`$E4`, `$E8`-`$EE`, `$F1`, `$F3`-`$F5`, `$FA`, `$FF` | no handler, 1 operand (no pan, fill, modulation) | various | - |
  | `$E5` `$E6` | alter volume | tempo div / alter volume | - |
  | `$E7` | no attack (FM only) | same | 256 |
  | `$EF` | set voice | same | 131 |
  | `$F0` | **set volume** (absolute: `CoordFlag.SET_VOL`) | modulation | 16 |
  | `$F2` | stop | same | 25 |
  | `$F6` `$F7` `$F8` | jump, loop, call | same | 65, 199, 94 |
  | `$F9` | return | FM1 release rate | 34 |
  | `$FB` | transposition (add) | - | 2 |
  | `$FC` | slide mode on / off | - | drums only |
  | `$FD` | raw-frequency mode (note = fnum word) | - | - |
  | `$FE` | FM3 special mode, 4 operands | - | - |

- **Voice:** 26 bytes: `$B0`, `$B4` (pan, AMS, FMS: setting the voice pans the track), TL x4,
  DT/MUL, RS/AR, AM/D1R, D2R, SL/RR.  Volume adds to the carriers' TL, as Sonic 1.
- **Pitch:** its own FM table (Z80 `$07D9`, indexed from note `$80`): `$81` = C0 as Sonic 1, every
  note above 6-18 cents flat (mostly -12).  The song's rules carry it (`rules.fm_frequencies`).  PSG table
  `$074D` (no song uses the PSG).
- **Tempo:** TempoWait as Sonic 1, but tempo 0 never stalls (Death Adder: `NO_TEMPO_HOLDS`), and
  the first hold comes **a frame late**: the counter is loaded as the song starts, after that
  frame's tempo check (`$06CA` runs before `$043A`).  `rules.tempo_phase` 1; FM1's key-ons on the rips'
  frames: Sutakora 128/128 (1/128 at Sonic 1's phase), Showdown 245/245, Wilderness 235/235.
  NTSC: V-int; PAL: YM timer B `$CB` (62.8 Hz).
- **Run-out:** a note keyed 256 frames without an attacking read is keyed off (`$00E8`: the fill
  counter, never set, wraps; a tie's read neither resets nor counts).  The rips key Death Adder's,
  The Battle's and Conclusion's long FM1 notes off 258-260 frames after the attack
  (`core/smps/run_out.py`).
- **Frequency writes:** every FM channel's, every frame.  A rip shows no read at a tie: the
  yardstick merges ties that change nothing compared, the pitch audit segments at pitch changes.

**Drums** (`type0fm/drums.py`, Z80 `$0899`)
- The drum track's note `$8n` starts FM drum n on FM3 (bits 4-6 a PSG drum: no song plays one).
  A drum is a record (`$096B`, 14: program, transpose, volume, voice index into `$0987`) and a
  program: track bytecode in the driver, at divider 1, its level the record's volume plus the drum
  track's.  The tables are found by the code that reads them.
- Programs use slide mode (`$FC 01`: note, slide, a skipped byte, duration; a signed fnum step a
  frame, the octave wrapping at `$27E` / `$4FE`) and `$E7` tie chains of 1-2 frame notes.  Drum 9
  is a rest (a hit only stops the drum before it); drum 10 never stops (it runs into voice data).
- Run frame by frame, each program matches the rips' FM3 exactly: all 4477 hits in the 11 rips with
  drums, at some TempoWait phase.  Each is rendered whole as a one-shot sample
  (`samples.drum_root`); a silent drum's hits are `C00`.

**Against the rips** (`configs/golden_axe/`, `rips.yaml`)
- `vgm_pitch_audit`: all 13 songs, 14345 notes right.
- `vgm_lift --skip FM3`: FM matches exactly on The Battle, Battle Field, Turtle Village 2,
  Showdown, Conclusion, Sutakora and Game Over; the rest differ at the first note and the loop.
  Path of Fiend matches to 38 s, where the rip's hold cycle shifts a frame (a lost or extra
  V-int: the rip's or the hardware's).  Death Adder: one tie to another pitch the lift cannot see.

---

## SMPS 68k Type 1b, MUCOM-style track code: Streets of Rage

`MK 00001019-01` (Rev A, *Bare Knuckle*), SHA-1 `731cdf18…`.  SMPS 68k's engine reading MUCOM-style
track code.  No disassembly: its driver (`$72800`-`$73C16`) read with capstone, the Z80 player with
z80dis; every fact below checked against the 15 rips (`reference/vgz/streets_of_rage_1/`, paired in
`configs/streets_of_rage/rips.yaml`).  The plan and its history: `docs/todo/done/streets_of_rage.md`.

**As Sonic 1:** sound RAM `$FFF000`, queue `$FFF00A`-`C`, IDs (music `$81`, SFX `$A0`, `$E0`-`$E3`
commands), priority table, `$30`-byte tracks (FM1-FM5 `$FFF040`, DAC `$FFF130`, PSG `$FFF160`), the
Z80 DAC mailbox (`$A01FFF`), the relative jump.  Different: track bytes, flags `$F0`-`$FF` (one
table per kind of track), no tempo (a tick is a frame), loops on a per-track stack, an absolute
volume step table, voices in register order.

**Where things are** (`mucom/variant.py`: each table found by the code that reads it)

| What | ROM | Note |
|---|---|---|
| Driver entry | `$72914` | V-int; PAL: a second call every 6th frame (58.3 Hz) |
| Music index | `$7288C` | 17 absolute longs, `$81`-`$91`; `$8D` = `$8E` |
| SFX index | `$73B56` | 48 longs `$A0`-`$CF`: listed, not read (music only) |
| FM octave | `$732D2` | 12 words C-B (`$284` ... `$4C0`); block = octave: 97 words, C0-B7 |
| PSG table | `$73A64` | 10 rows of 12; rows 2-6 are Sonic 1's (checked) |
| FM volume | `$73600` | carrier TL by step 0-20; songs step to -4 (`36 33 30 2D` before it) |
| Carrier masks | `$73728` | per algorithm |
| PSG envelopes | `$728D0` | 5; `$81` holds, `$80` restarts, `$83` silences (`EnvelopeCommand.MUTE`) |
| Flag tables | `$73302` FM and drums, `$73342` PSG | |
| Z80 player | `$795A2` | Kosinski, unpacked to `$FF7000`, then copied (`z80_ram`'s buffered Kosinski) |

**Song header:** `voices.w fm.b psg.b`; FM entries `ptr.w volume.b` (FM1-FM5, then the drum track);
PSG entries `ptr.w volume.b envelope.b` (tone 3, 2, 1).  No tempo, no pitch.

**Track code** (`mucom/grammar.py`): `d n` a note (`d` $01-$7F frames, `n` octave | semitone),
`$80|d` a rest, `$00` the end, `$F0`-`$FF` a flag.  Durations stop at 127: longer notes are tie
chains.  FM octave o plays block o; PSG row r plays octave r + 1 (rows 0-1 read row 2, 6-9 row 6).

| Byte | FM | PSG | Drum track | The IR |
|---|---|---|---|---|
| `$F0 n` | voice n | - | notes play sample `$80` \| n | `SetVoice`, `SelectSample` |
| `$F1 v` | volume step v | att (-v & 15) + header volume | - | `VolumeStep` -> `SetVol` |
| `$F2 lo hi m` | detune word: m 0 sets, else adds | the word >> 4 | - | `Detune`, `DetuneAdd` |
| `$F3 n` | gate: key off n frames before the end | same | cuts the sample | `Gate` -> note + rest (`cut`) |
| `$F4 0 d s w c` | vibrato: delay, speed, depth word, count | the sum >> 4 | - | `ModSet` (count + 1) |
| `$F4 1` / `2` | vibrato off / on | same | - | `ModOff`, `ModOn` |
| `$F5` / `$F6 x n o` / `$FE o` | loop start / end, n passes / leave on the last pass | same | same | label, `LOOP`, `LOOP_EXIT` |
| `$F7 a b c d` | FM3 special mode: OP4, OP3, OP2, OP1 FNUM offsets | white noise at tone 3's rate | - | `Fm3Special`, `PsgForm` |
| `$F8 n` | pan: 1 L, 2 R, else C | - | pan | `Pan` |
| `$FA r v` | YM register write: the voice patched | envelope | YM write | `VoiceRegister` |
| `$FB n` | volume += n steps | att -= n | - | `AlterVolumeStep`, `AlterVol` |
| `$FC f p a` | LFO: `$22` = f \| 8, B4 FMS p AMS a | hangs the driver | LFO | `Lfo` |
| `$FD` | tie | same | same | `NO_ATTACK` |
| `$FF w` | jump; clears the tie (FM, drums) | jump | jump | `JUMP` |

**How it plays** (each kind of track's `TrackRules`, `core/smps/driver_track.py`)
- **Volume:** FM carrier TL = table[step] + header volume (`add.b`), absolute: a voice's own carrier
  TLs never play (`VoiceLayout.carrier_tl`).  A `$FB` in a loop crescendos.  PSG: att + envelope
  step, clamped to 15.
- **Detune:** added to the 14-bit block|fnum word, the PSG's sum >> 4 (`word_shift`); up to +195,
  several semitones (detune variants per pitch class: `docs/pipeline.md`).  A word past the PSG's
  10 bits wraps (`$8F` PSG1).
- **Ties and the gate:** every note keys off, then on; after `$FD` neither: a new pitch slides
  legato, written as a frequency alone.  The gate keys off n frames before the end, but not a
  note the next byte ties (`gate_sees_tie`) nor, on FM, a tied note (`gate_spares_tied`); a note
  no longer than n plays whole.  A rest after a tie keys FM off a frame in, the PSG at once
  (`tied_rest_holds`).  A jump drops an FM or drum track's tie, not a PSG's (`replay_tie`).
- **Vibrato:** SMPS's shape, but the count is tested before it counts down and the turn's step
  moves too: a half cycle is count + 1 moves, no pause (`modulation_turn_pause`; Sonic 1's turn
  adds nothing).  The PSG adds the depth words' sum >> 4 (`word_shift`), not each step's.  Cycles
  too slow for `4x1` play as slides.
- **Voices:** 25 bytes, DT/MUL TL KS/AR AM/D1R D2R D1L/RR in register order, FB/ALG last.  `$FA`
  writes (D1R, D2R, D1L/RR; 203 in the songs) last until the next voice: a patched copy
  (`voice_patch.py`).  `$FA $24`-`$26` (MUCOM's timers) are inert.
- **FM3 special mode:** `$F7` stores four offsets the FM3 track's frequency writes add per
  operator (`$72CAE`: A6/A2 OP4, AC/A8 OP3, AE/AA OP2, AD/A9 OP1) and sets `$27` = `$40` when any is
  nonzero; the mode is never cleared, and zeros sound as normal mode.  Only OP4 +100, on FM3 drums
  (`$84 $88 $89 $8C $8D $90`).  A voice copy carrying the offsets, rendered on channel 3.
- **LFO:** `$22` is the chip's, so a note plays at the frequency the last `$FC` of any track wrote
  (`core/smps/lfo.py`: `$88`, `$8B` depend on it); B4's sensitivity survives voice sets and pans.
  A voice copy under the LFO; its loop spans whole LFO cycles.  Not modelled: a jump centres B4's
  RAM copy (pan, LFO) for the next voice set; every song sets both after its loop target.
- **PSG noise:** `$F7` on tone 3 writes no tone 3 frequency, so a noise note plays the last tone
  note's divider, or 0 (`nMaxPSG`) before any.

**DAC** (`mucom/dac.py`; the Z80 player: table at Z80 `$019B`, 5 bytes: start.w, size.w, pitch.b)
- Music samples `$81`-`$84`; `$85` is empty (size 0): the drum track's rests and gates play it, so
  they **cut** the sample (`TrackRules.rest_cuts`: `C00`).  From `$86`: voice clips, not read.
- Format: each sample its own 16-byte delta table (byte 0 = a run length); nibble n adds table[n],
  nibble 0 repeats the last delta table[0] times.  Byte-equal to every rip bank that holds them.
- Rates by cycles per output (a literal nibble 190 / 242 + 13 per pitch step, a run step 219 + 13),
  less the 68k's 1.6 % stall: `$81` 12983 Hz, `$82` `$84` 15381, `$83` 9464.

**Data quirks**
- `$89` Stealthy Steps' noise track: `$F6` at `$7E196` closes a loop never opened.  The walk unrolls a
  loop from its `$F6`, so it plays as written (no fix); its PSG3 (5173 frames) drifts against the
  5120 loop (`loop_drift`).
- Tracks loop at unequal lengths (`$8A`, `$8C`, `$8F`, `$91`): the song is replayed to their common
  period (`docs/pipeline.md`).
- **Driver bug, not simulated:** the drum gate fires on a rest too and saves `$85` over the saved
  sample, silencing hits after it until the next `$F0`.  The rips miss them (`$85` 32 hits, `$89` 64,
  `$8B` 23).

**Against the rips** (`configs/streets_of_rage/`: 16 minimal configs)
- `vgm_lift`: FM onsets and lengths equal on all 15 but the logs' last frames.  Its `note` differs
  where it cannot read: a detune, special mode, a key-less slide.
- `vgm_frames`: FM pitch, level and voice registers exact at every key-on, special mode included.
- `vgm_pitch_audit`: clean but where slots run out (`$88` FM3 B notes, `$90`: copies take the slots
  before detune variants, `copy_no_slot` / `detune_no_slot`), `$8B` re-struck ties a row late, and
  `$91`'s first second (the rip starts 39 frames in).
- Volumes measured (`measure_volumes.py --configs configs/streets_of_rage`, one pass).  Left: FM3's
  one-frame G2 hits (gate 6 of 7) -27 ... -30 dB; noise samples played with another envelope.

- Vibrato (`vgm_compare`, 15 rips): rate mismatches 85 -> 7 once the turns and the PSG's sum were
  read as above (Moon Beach's PSG: 6 Hz, was played at 4.8, twice as deep).  Left: swings under the
  MOD's smallest 4xy (0.7 periods: PSG and FMS 2, 5-8 c), slow sweeps the estimator reads as
  beats (played as slides), rows whose effect slot a `Cxx` / `Axy` takes at speed 2 (`$8C`).  A
  hardware LFO is baked into its copy's sample, so its rate follows the note across a window and
  restarts with each re-struck tie (`legato: retrigger`: `$8B` FM4).

**Not done:** the SFX; listening and Amiga merged builds (4.4).

---

## Adding a variant

1. Probe: find the driver (68k code, or the Z80 blob the 68k copies), its flag jump table, the
   song index, a song header and a voice.  Compare each table with Sonic 1's.
2. Code: a folder in its family (`core/drivers/smps68k/<driver>/`, `smpsz80/<driver>/`):
   `variant.py`, the `SmpsVariant` (memory, locate, flags per kind of track, track grammar, header
   and voice layouts, envelope commands, its `PlaybackRules` - Sonic 1's `SONIC1_RULES`
   (`core/drivers/reference.py`) with what differs replaced - DAC, FM table, FM drums), and
   whatever only this driver has; what two drivers of a family share moves up to the family
   folder.  Name it in `core/drivers/names.py` and `registry.py` (loaded on first use; the family
   `__init__` imports no driver); each ROM it is known in, with any data fixes, in `games.py`.  A driver imports the framework
   absolutely (`core.rom.flags`), its family relatively.  Nothing outside its folder names it:
   what the song itself must say goes in the IR (`core/smps`), what a reader needs in the
   variant's description - never a variant check.
3. Check: `tools/rom_import.py` lists every song; `tools/vgm_lift.py` against the rips (FM is what
   it reads in full); `tools/vgm_pitch_audit.py`; `tools/measure_volumes.py`.  A rip is only as good
   as its emulator and ripper: back a finding with the driver's code.
