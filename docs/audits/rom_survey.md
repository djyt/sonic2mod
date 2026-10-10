# ROM survey: which Mega Drive games the drivers can read (2026-10-10)

A No-Intro-style US set (`C:\emu\megadrive\sega-genesis-romset-ultra-usa`, 1,515 files) run
through the drivers in `core/drivers/`: which ROMs hold an SMPS driver the tooling detects, which
of those an existing driver reads, and - with two broader ways of finding a driver, tried in a
scratchpad - what stops the rest.  A report: no code was changed.  The variants named here are
described in `docs/smps_variants.md`.

"Reads" throughout means what detection means (`core/drivers/detect.py`): every song decodes -
each coordination flag known, no code starting inside an instruction, every pointer inside the
ROM.  It does not mean a song converts correctly: tempo, voices and DAC drums are unverified for
every ROM not listed in `core/drivers/games.py`.

---

## 1. The tooling as it stands

Each ROM loaded with `RomImage.load`, then every driver's `locate` and `first_failure` - what
`detect_variant` does, but recording each driver's outcome rather than stopping.

| Category | Files |
|---|---|
| Not loaded (no `SEGA` at `$100`: 21; an interleaved `.smd`; two `.html`) | 24 |
| No SMPS signal the tooling sees | 1,389 |
| A signal (FM table) but no driver locates the indexes | 59 |
| Located, but some song fails to decode | 22 |
| **Exactly one driver reads every song** | **21** |
| More than one driver reads every song | 0 |

### Read (21 files)

| Driver | ROMs |
|---|---|
| `sonic1` | Sonic 1 (USA, Europe), its Classic Collection copy, Sonic Classics, 6-Pak, Sonic 2 Beta (June 1992) |
| `smps68k_type1a` | Moonwalker Rev A, **Shadow Dancer (World)**, Shadow Dancer (Steam), **Toki - Going Ape Spit** |
| `smpsz80_type0fm` | Golden Axe ×5 (World, Rev A, Rev B Smash Pack, betas 1989-10-14 and 1989-11-22), Mega Games 2, Mega Games 6 Vol. 2 |
| `smps68k_mucom` | Streets of Rage (World), Rev A |
| `smpsz80_sh2` | Space Harrier II |
| `smpsz80_outrun` | OutRun, its Smash Pack copy |

- The **Sonic 2 June 1992 beta** carries Sonic 1's sound bank: its 19 songs are Sonic 1's byte
  for byte, at the same address (`$745DC` for `$81`).  None of Sonic 2's own music is read.
- The set's **Sonic 1 is Rev 00**, not the Rev 01 `games.py` lists: it is read by detection, so
  without `_SONIC1_REV01_FIXES`.
- **Shadow Dancer (World) (Beta) (1990-10-02)** has a blank header (`$FF` at `$100`), which
  `RomImage.load` refuses; read as `RomImage(data)`, Type 1a reads all 19 songs.
- New to the converter, untested against rips: Shadow Dancer, Toki.

### Located, not read (22 files)

| ROM | Driver | Songs read | First stop |
|---|---|---|---|
| Golden Axe III | Type 1a | 26/27 | `$E9` LFO |
| Alien Soldier (Virtual Console) | Type 1a | 29/31 | `$FD` SSG-EG |
| Light Crusader (+ beta) | Type 1a | 29/32 | `$FD` SSG-EG |
| Mighty Morphin Power Rangers - The Movie (+ 4 betas) | Type 1a | 16/20 | `$FD` SSG-EG |
| Yuu Yuu Hakusho - Makyou Toitsusen | Type 1a | 22/31 | `$FD` SSG-EG, `$E9` LFO |
| Dynamite Headdy (+ beta) | Type 1a | 13/42 | `$FD` SSG-EG, `$E9` LFO |
| Phantasy Star IV (+ 4 betas, sample) | Type 1a | 4/52 | `$E3` global flag, `$E5` |
| Saint Sword | Type 0 FM | 14/15 | code inside an instruction |
| Whip Rush (+ beta) | Type 0 FM | 7/15 | code inside an instruction |
| Cadash | Type 0 FM | 8/15 | `$FC` slide mode |

Every 68k one fails under `sonic1` at a flag Sonic 1 lacks (`$FA`-`$FD`) and under Type 1a at a
flag Type 1a knows but refuses: the same flag set, short of a few effects (§4).

### A signal, not located (59 files)

- **Sonic 1's FM octave in the ROM, no Go_ block (21):** Gunstar Heroes, Shinobi III (+ beta),
  Ristar (+ beta), Strider (+ Virtual Console), Golden Axe II, Fatal Fury, Fatal Fury 2, Samurai
  Shodown, Deadly Moves, Chester Cheetah, McDonald's Treasure Land Adventure, Dexstar ×2, Sailor
  Moon, Mega Games 3, Mega Games 6 Vol. 3, Arcade Legends, a Sonic 4-in-1 hack.  The octave is
  the standard Yamaha one: not proof of SMPS by itself.
- **One Z80 program with an FM table, no Z80 locator matches (38):** the Sonic 3 / Sonic &
  Knuckles family (16 files: SMPS Z80 Type 2, no driver); early Sega titles - Mystic Defender ×3,
  Columns ×2, Super Thunder Blade, Ghouls'n Ghosts ×3, Alien Storm, Arnold Palmer Tournament Golf
  ×2, Dynamite Duke ×3, Phantasy Star III ×2, Rambo III; and Shadow Blasters, Mercs, OutRun 2019
  ×2, Zoom!, Tecmo World Cup, King of the Monsters 2.

Near misses in the "no signal" group:
- **The Revenge of Shinobi (5 files):** MUCOM's code patterns match, but its music index points
  at no header - Yuzo Koshiro's earlier driver, Streets of Rage's likely ancestor.
- **Compilations** (6-Pak, Mega Games 3, 6, 6 Vol. 3): two Z80 programs hold an FM table, and
  `driver_ram` requires exactly one - 6-Pak's Golden Axe is missed (its Sonic 1 is read).
- Micro Machines Turbo Tournament 96, Premier Manager ×2, Ultimate Soccer, Sega Sports 1: MUCOM's
  code pattern by coincidence, then a read outside the ROM.

### Where detection stops

1,413 of the 1,491 loadable files stop at `z80_loads`: "no load of a Z80 program into Z80 RAM
found".  `core/rom/z80.py` knows five 68k copy loops, one per game supported so far; a driver
compressed (all 14 Sonic 2 files: Saxman) or copied by any other loop is never seen.  On the 68k
side, `locate_68k` needs Sonic 1's six-long Go_ block.

A raw search for SMPS-style tables, outside the tooling (Sonic 1's FM octave and PSG dividers,
any 6 consecutive words, either byte order), puts 93 files (57 titles) in the "no signal" group
with a little-endian FM octave and the PSG table: a Z80 driver the loader patterns miss.  Among
them Bonanza Bros., Castle of Illusion, QuackShot, World of Illusion, Columns III, ESWAT,
Flicky, Gain Ground, Super Monaco GP, Fatal Labyrinth, Arrow Flash, DJ Boy, Marvel Land, Sonic
3D Blast - and, since the tables are equal temperament, non-SMPS games too (Battletoads, Mega Man
- The Wily Wars).

---

## 2. Finding the driver by content

Two experiments, each a fallback for one of the gates above.

**Z80: the driver found by what it holds.**  Where the ROM holds a little-endian FM octave, each
offset up to `$1F00` before it that starts like a Z80 program (`di` then `ld sp` / `jp` / `im` /
`ld a` / `xor a` / `ld hl` / `jr`) is tried as the driver at Z80 `$0000` - `z80_loads`
replaced by that one program - under the Type 0 FM, SH2 and OutRun locators.  About 30 lines.

**68k: the music index found by what it points at.**  Runs of longs (top byte 0) each pointing
at a plausible 68k music header (`is_music_header`, `HEADER_68K`), Go_ block or not, decoded as
an index from `$81`.  This needs a shape test: Type 1a defines nearly every flag byte, so data
that is no index "decodes" too.  A run counts when its pointers are distinct (80 %), the median
song has 4 or more tracks, and the median track at most 1,500 instructions.  Without it, the Sonic 2 June
beta showed a "second bank" of 25 songs at `$78B44` - every pointer into Sonic 1's data, 2 tracks
of 13,000+ instructions each.  The test drops that, and Ristar, Dexstar, Revenge of Shinobi, Strider,
Splatterhouse 3, Streets of Rage 3 and Mega Games 3 with it.

Together: **48 titles** with a located index, **25 read completely** - 12 more than §1:

| Found by | Driver | Read, new |
|---|---|---|
| Z80 scan | Type 0 FM | Air Buster, DJ Boy, Mazin Saga, Smart Mouse, Legend of Wukong (Unl), Generals of the Yang Family (Unl), Super Mario Bros. (Unl pirate); Golden Axe inside Arcade Legends and Mega Games 6 |
| 68k scan | Type 1a | Ayrton Senna's Super Monaco GP II (19 songs), Sailor Moon (31), Chester Cheetah (15) |

The 68k-scan finds deserve more suspicion than the Z80 ones: Type 1a reads almost any byte.
Super Monaco GP II's run (`$E0510`, 7-8 tracks, 250-2,800 instructions a song) and Sailor Moon's
(`$22BD0`, 9 tracks) look like music; each needs its rip before it is believed.

---

## 3. Every blocker, every song

The located songs decoded again leniently: a flag the driver refuses (its length known) is
skipped and recorded instead of ending the song; a hard error (unknown flag, misaligned code, a
pointer outside the ROM) still ends it and is recorded.  So each song lists everything that stops
it, not only the first.  Titles group revisions and betas (the best-reading file stands for its
title); between `sonic1` and Type 1a the driver with fewer hard errors is kept, and an index from
a driver's own locator is preferred to a scan's.

### `$FD` in the later 68k games takes no operands

Nearly every misaligned song in the 68k titles fails right after `$FD`, which Type 1a reads as
SSG-EG with 4 operand bytes (Moonwalker's).  Tried with 0, 1 and 2:

| Title | as Type 1a (4) | 0 | 1 | 2 |
|---|---|---|---|---|
| Dynamite Headdy | 36/42 | **42/42** | 33/42 | 37/42 |
| McDonald's Treasure Land Adventure | 16/24 | **24/24** | 17/24 | 16/24 |
| Yuu Yuu Hakusho | 26/31 | **31/31** | 27/31 | 26/31 |
| Shinobi III | 25/28 | **28/28** | 27/28 | 27/28 |
| Fatal Fury 2 | 23/25 | **25/25** | 24/25 | 24/25 |
| Fatal Fury | 16/19 | **18/19** | 16/19 | 16/19 |
| Samurai Shodown | 26/27 | **27/27** | 27/27 | 27/27 |
| Mighty Morphin Power Rangers - The Movie | 19/20 | **20/20** | 20/20 | 20/20 |
| Splatterhouse 2 | 11/12 | **12/12** | 11/12 | 11/12 |

(Songs with no hard error.)  With no operands the misalignments vanish in eight titles and in all
but one of Fatal Fury's songs: these games - Treasure's, Sega's later ones, the Fatal Fury and
Samurai Shodown ports - share a later 68k dialect in which `$FD` is
a 1-byte flag.  What it does there is not known; a disassembly of one of their drivers would say.
Type 0 FM already has a `$FD` toggle (raw-frequency mode, Cadash), which is one candidate.

### Per title (with `$FD` as a 1-byte flag in the Type 1a titles)

| Title | Files | Found by | Driver | Songs read | Blockers (songs) |
|---|---|---|---|---|---|
| 6-Pak | 1 | locator | Sonic 1 | 19/19 | - |
| Air Buster | 1 | Z80 scan | Type 0 FM | 15/15 | - |
| Arcade Legends Sega Mega Drive | 1 | Z80 scan | Type 0 FM | 15/15 | - |
| Ayrton Senna's Super Monaco GP II | 1 | 68k scan | Type 1a | 19/19 | - |
| Bare Knuckle ~ Streets of Rage | 2 | locator | MUCOM | 17/17 | - |
| Chester Cheetah - Too Cool to Fool | 1 | 68k scan | Type 1a | 15/15 | - |
| DJ Boy | 1 | Z80 scan | Type 0 FM | 15/15 | - |
| Generals of the Yang Family | 1 | Z80 scan | Type 0 FM | 14/14 | - |
| Golden Axe | 5 | locator | Type 0 FM | 15/15 | - |
| Legend of Wukong | 1 | Z80 scan | Type 0 FM | 11/11 | - |
| Mazin Saga - Mutant Fighter | 1 | Z80 scan | Type 0 FM | 15/15 | - |
| Mega Games 2 | 1 | locator | Type 0 FM | 15/15 | - |
| Mega Games 6 | 1 | Z80 scan | Type 0 FM | 15/15 | - |
| Mega Games 6 Vol. 2 | 1 | locator | Type 0 FM | 15/15 | - |
| Michael Jackson's Moonwalker | 1 | locator | Type 1a | 23/23 | - |
| OutRun | 2 | locator | OutRun | 5/5 | - |
| Sailor Moon | 1 | 68k scan | Type 1a | 31/31 | - |
| Shadow Dancer - The Secret of Shinobi | 3 | locator | Type 1a | 19/19 | - |
| Smart Mouse | 1 | Z80 scan | Type 0 FM | 12/12 | - |
| Sonic Compilation ~ Sonic Classics | 1 | locator | Sonic 1 | 19/19 | - |
| Sonic The Hedgehog | 2 | locator | Sonic 1 | 19/19 | - |
| Sonic The Hedgehog 2 (Beta, June 1992: Sonic 1's songs) | 1 | locator | Sonic 1 | 19/19 | - |
| Space Harrier II | 1 | locator | SH2 | 25/25 | - |
| Super Mario Bros. (Unl) | 1 | Z80 scan | Type 0 FM | 10/10 | - |
| Toki - Going Ape Spit | 1 | locator | Type 1a | 16/16 | - |
| Golden Axe III | 1 | locator | Type 1a | 26/27 | `$E9` LFO (1) |
| Alien Soldier | 1 | locator | Type 1a | 29/31 | `$FD` (2); `$FC` clears `$E3` (1) |
| Saint Sword | 1 | locator | Type 0 FM | 14/15 | misaligned (1) |
| Samurai Shodown | 1 | 68k scan | Type 1a | 25/27 | `$FC` clears `$E3` (2); `$FD` (1) |
| Light Crusader | 2 | locator | Type 1a | 29/32 | `$FD` (3) |
| Gunstar Heroes | 1 | 68k scan | Type 1a | 24/27 | `$FC` clears `$E3` (3); `$FD` (3) |
| Wonder Boy III - Monster Lair | 1 | Z80 scan | Type 0 FM | 12/14 | misaligned (1); `$FE` FM3 special (1) |
| Golden Axe II | 2 | 68k scan | Type 1a | 13/16 | `$E5` FM + PSG volume (3) |
| Mighty Morphin Power Rangers - The Movie | 5 | locator | Type 1a | 16/20 | `$FD` (4) |
| Shinobi III - Return of the Ninja Master | 2 | 68k scan | Type 1a | 20/28 | `$FD` (4); `$E3` global flag (4); `$FE` FM3 special (1) |
| Yuu Yuu Hakusho - Makyou Toitsusen | 1 | locator | Type 1a | 22/31 | `$FD` (8); `$E9` LFO (1) |
| Fatal Fury | 1 | 68k scan | Type 1a | 13/19 | `$FD` (5); misaligned (1) |
| Fighting Masters | 1 | Z80 scan | Type 0 FM | 8/12 | misaligned (2); `$FC` slide mode (1); pointer outside the bank (1) |
| Deadly Moves | 1 | 68k scan | Type 1a | 9/15 | `$FC` clears `$E3` (6) |
| Fatal Fury 2 | 1 | 68k scan | Type 1a | 15/25 | `$FD` (10) |
| Cadash | 1 | locator | Type 0 FM | 8/15 | `$FC` slide mode (7); `$FD` raw frequency (1); `$FE` FM3 special (1) |
| Whip Rush | 2 | locator | Type 0 FM | 7/15 | misaligned (8) |
| Dynamite Headdy | 2 | locator | Type 1a | 13/42 | `$FD` (27); `$E9` LFO (13); `$E5` FM + PSG volume (1) |
| McDonald's Treasure Land Adventure | 1 | 68k scan | Type 1a | 4/24 | `$FD` (17); `$E9` LFO (9); `$E3` global flag (4) |
| Pat Riley Basketball | 2 | Z80 scan | Type 0 FM | 1/11 | misaligned (9); `$FC` slide mode (1) |
| Phantasy Star IV | 6 | locator | Type 1a | 4/52 | `$E3` global flag (47); `$FC` clears `$E3` (8); `$E5` FM + PSG volume (3); misaligned (1) |
| Gauntlet IV | 2 | 68k scan | Type 1a | 0/6 | `$E3` global flag (4); misaligned (2); `$FC` clears `$E3` (2) |
| Splatterhouse 2 | 2 | 68k scan | Type 1a | 0/12 | `$E5` FM + PSG volume (12); `$E3` global flag (11); `$FE` FM3 special (8); `$FD` (7); `$E9` LFO (3); `$FC` clears `$E3` (2) |

Gauntlet IV and Splatterhouse 2 come from the 68k scan alone and refuse at nearly every song:
possibly not SMPS at all.

---

## 4. What to implement, in order

Each step the flag that completes the most titles (then frees the most songs), over the 23
blocked titles (516 songs):

| # | Flag | Titles completed | Songs read |
|---|---|---|---|
| | (as found) | | 312 |
| 1 | `$FD` as a 1-byte flag (later 68k) | +3: Fatal Fury 2, Light Crusader, Power Rangers The Movie | 371 |
| 2 | `$FC` clears `$E3`'s flag | +4: Alien Soldier, Deadly Moves, Gunstar Heroes, Samurai Shodown | 383 |
| 3 | `$E9` LFO | +2: Golden Axe III, Yuu Yuu Hakusho | 405 |
| 4 | `$E5` FM and PSG volume in one flag | +2: Dynamite Headdy, Golden Axe II | 410 |
| 5 | `$E3` sets a global flag | +1: McDonald's; 61 of Phantasy Star IV's songs | 471 |
| 6 | `$FE` FM3 special mode | +2: Shinobi III, Splatterhouse 2 | 481 |
| 7 | `$FC` slide mode (Type 0 FM) | - | 489 |
| 8 | `$FD` raw-frequency mode (Type 0 FM) | +1: Cadash | 490 |

Implementing means converting each flag, not skipping it: `$E3` / `$FC` are not understood yet
(what reads the global flag), `$FD` in the later dialect not at all.  SSG-EG, LFO and FM3 special
mode are chip features the OPN2 core renders; how each fits a baked sample is the open question.

**Left after all eight (8 titles)** - hard errors, each an operand count to set per dialect:
- Whip Rush (8 songs) and Saint Sword (1): the misaligned code starts 1 byte into a `$E6` / `$E0`
  - those flags most likely take no operand in these games (Type 0 FM gives them one).
- Phantasy Star IV (1 song): 1 byte into a `$FF`.
- Pat Riley Basketball (9), Fighting Masters (2 + a pointer outside the bank), Wonder Boy III (1),
  Fatal Fury (1), Gauntlet IV (2): not traced.

---

## 5. Summary

- **Not bespoke, mostly.**  Two families hold 41 of the 48 located titles: Type 0 FM and its
  siblings (Z80, 18), and Type 1a with its later dialect, `$FD` a 1-byte flag (68k, 23).  What separates them from
  the drivers that exist is a short list of flags (§4) and a few operand counts.
- **Detection is the larger gap.**  Most ROMs stop before any SMPS check: `z80_loads` knows five
  copy loops and `locate_68k` one Go_ block shape.  Finding the Z80 driver by content alone reads
  9 more Type 0 FM ROMs with no driver change.
- **Still out of reach:** Sonic 2 (a compressed Z80 driver, SMPS Z80 Type 1), Sonic 3 / Sonic &
  Knuckles / Sonic 3D Blast (Type 2), Ristar / Dexstar (not located once the 68k scan has its
  shape test), The Revenge of Shinobi (Koshiro's driver), and about 1,200 files with no SMPS
  signal - most of them other companies' drivers.
- Smaller fixes: a `RomImage` that loads a ROM without `SEGA` at `$100`, with a warning (Shadow
  Dancer's beta); compilations, by naming which driver copy a config reads.

The scripts were scratchpad work and are not in the repository; §2 and §3 say enough to rebuild
them: `locate` + `first_failure` per driver; the two scans; lenient decoding by turning each
`refuse()` into `drop()` in a copy of the variant's flag table (`dataclasses.replace`).
