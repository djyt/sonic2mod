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
Streets of Rage's facts are in `docs/todo/streets_of_rage.md` until its conversion is done.

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
