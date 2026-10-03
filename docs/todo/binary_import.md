# Binary import: SMPS bytecode read from a Mega Drive ROM

Planned 2026-10-03.  Goal: `convert.py` reads a song straight from a ROM whose driver is a known
SMPS variant, starting with Sonic 1 (`input/roms/sonic_rev01.bin`), then other games with a
similar driver.

Status key: `[ ]` open, `[x]` done.  A done item keeps what was measured and drops its plan.

---

## Approach

The bytecode is the score: notes, durations, flags, calls, loops, voices, tempo.  The VGZ lift
(`vgz_conversion.md`) has to infer all of these, and some of them can't be observed at all.  So the
ROM is the input, and the VGZ becomes the yardstick, and the way to find songs in a ROM no one has
disassembled.

```
  .asm  ──SmpsParser (text → SmpsCode)──┐
                                        ├──> walk (one) ──> SmpsSong ──> converter, unchanged
  ROM   ──rom decoder (bytes → SmpsCode)┘

  .vgz  ──lift_song──> SmpsSong ──> played_song ──┐
                                                  ├──> compare_songs (the yardstick)
  ROM / asm ──────────> SmpsSong ──> played_song ──┘
```

**One walker.**  The parser mixes two jobs: reading text, and the driver's reading rules (a
pending note's duration, `smpsNoAttack`, unrolling `smpsLoop`, inlining `smpsCall`, where a
loop's tick lands).  Split them, and the ROM decoder supplies only the first job.  Rejected:
bytes → asm text → parser.  Text loses the addresses, and it means writing a writer before any
reading works.  The writer comes later as `--asm` (1.7), where a round trip is a check on both.

**Ground truth.**  The disassembly's asm assembles to this ROM, except where the disassembly
fixes bugs: `SmpsParser._CONDITIONAL_DEFAULTS` sets `FixMusicAndSFXDataBugs: True`, but the ROM
(and every VGZ) is the game as shipped.  Two songs differ: Marble Zone's PSG3 (three notes
off the PSG table) and Credits (three extra rests, plus an `smpsAlterVol $0C` that mutes a
passage).

### What the ROM looks like (probed 2026-10-03)

- 512 KB, `SEGA MEGA DRIVE`, `SONIC THE HEDGEHOG`, serial `GM 00004049-01`.
- `FMFrequencies` at `$72790` and `PSGFrequencies` at `$729CE`.  Each is a unique match for the
  first row of `core/smps/driver_tables.py`'s table.
- The `Go_` block at `$71990` holds 6 longs: `SoundPriorities $71AE8`, `SpecSoundIndex $78C04`,
  `MusicIndex $71A9C`, `SoundIndex $78B44`, `SpeedUpIndex $71A94`, `PSG_Index $719A8`.
- `MusicIndex` has 19 longs, `$81`–`$93`, GHZ at `$745DC`.  Its header: voices `+$687`, 6 FM
  (DAC included), 3 PSG, tempo `$01, $03`, as in the asm.
- Sonic 1 pointers (`SonicDriverVer = 1`): voice and channel pointers are relative to the song's
  start, and jump / loop / call targets are `word_address + 1 + signed word`.  Z80 drivers use
  absolute Z80 addresses instead.
- `smpsHeaderStartSong` emits no bytes.  Songs are found through the index, not a marker.
- DAC samples sit inside the Z80 driver, which is Kosinski-compressed.  The samples are DPCM
  (`sound/dac/dpcm/deltas.bin`), with timpani rates in `DAC_sample_rate`.

---

## Phase 0: groundwork

### [x] 0.1 ROM hygiene (done 2026-10-03)
`input/roms` is git-ignored.  ROM tests and regression cases skip without it and say so; a ROM is
named by its header and SHA-1 (`RomImage.serial`, `.sha1`; rev01 `1f1e480f…`).

### [x] 0.2 Split the parser (done 2026-10-03, `core/smps/code.py`)
`SmpsCode` (ops in source order) + `song_from_code`, the one walk; `SmpsParser` is the asm front
end.  All 87 asm parses (music, SFX, `input/`) repr-identical before and after; regression, tool
regression and unit tests unchanged.  `write_asm` (`core/smps/asm_writer.py`) is its inverse.

### [x] 0.3 Data-bug switch (done 2026-10-03)
`SmpsParser(fix_data_bugs=True)`; the parser now reads `if Symbol=0` too.  Before, it missed
Credits' `=0` block, so Credits converted as shipped and Marble Zone fixed.  Default chosen by the
user: fixed - the Credits baseline changed (PSG2's passage on time, its muting `smpsAlterVol` gone).
`tools/vgm_lift.py` compares rips with the shipped spelling.  Fixed vs shipped differ in Marble Zone
PSG3, Credits PSG2 and SndBC's header only.

---

## Phase 1: Sonic 1 (`sonic_rev01.bin`) - done 2026-10-03

`core/rom/`, beside `core/vgm/` (source → rom → smps).  `tools/rom_import.py ROM --compare sonic_1`:
68 of 68 read as their asm.

### [x] 1.1 ROM image and driver location
`locate_sounds`: the FM table confirms the driver; the PSG1 envelope → `PSG_Index` → `Go_` block
(`$71990`) → indexes, each ending at the next table or the first implausible header.  19 songs,
48 + 1 SFX.

### [x] 1.2 Headers / 1.3 Track decoder / 1.4 Voices
Every header, event and loop equals the asm's (`parse_differences`, shipped spelling).  Learned:
- `smpsFade` ($E4) and `smpsStopSpecial` ($EE) end the track (`addq.w #8,sp`), not just "ignored":
  decoding past them ran into the voice bank (Extra Life DAC, Waterfall).  Both front ends now stop.
- Voices equal on the chip's bits, not param for param: the asm writes TL $80 on modulators (Get
  Emerald, 9 SFX), AR $2F / D2R $A9 past the field.  The decoder reads fields as the chip does;
  `SmpsVoice.chip_registers()` (TL 7 bits) is what `played_song` and `parse_differences` compare.
- The bank's count is the highest `smpsSetvoice` + 1: Stage Clear's asm defines a 5th voice no
  track uses.
- A jump back to a channel's own start records the tick only (`loop_event_index` None), as the
  parser always has.

### [x] 1.5 Input dispatch
`input_file:` a ROM + `rom_song: "$81"`; `convert.py --input FILE --rom-song ID` overrides.
All 20 configs (and the 3 merged builds) write byte-identical MODs from ROM and asm (Marble Zone
and Credits against their shipped asm).

### [x] 1.6 SFX
`sonic2wav.py --rom FILE`: all 49 WAVs byte-identical to the asm path's (`--no-normalize`; SndBC's
fixed pitch $10 and shipped $90 render the same: the driver masks bit 7).

### [x] 1.7 Tooling
`tools/rom_import.py`: listing, `--compare DIR` (parse + `compare_songs`, music only: `played_song`
cannot follow SFX notes past the FM table), `--asm DIR` (all 68 parse back to the same song, labels
included), `--dac DIR`.  `vgm_lift.py`'s diff printing moved to `core/ui/song_diff.py`, output unchanged.

### [x] 1.8 DAC samples
`core/rom/dac.py`: the Z80 blob through the 68k's `lea DACDriver,a0 / lea z80_ram,a1` ($72E7C, 7110
bytes decompressed; `kosinski.py` from aonic's `kos_decom.py`), DPCM high nibble then low.  Kick,
snare, timpani byte-equal to `samples/*.raw`.  The VGZ banks hold what played, cut where the next
hit interrupts: exact only where a sample played whole (Title, GHZ, SYZ, LZ, SBZ, Ending, Continue,
Game Over).  Rates from the play loop's cycles (301 + 26 (pitch - 1) per byte): kick 8201 Hz, snare
23784, timpani 7328; $88-$8B pitches $12 $15 $1C $1D.  VGZ write rates read 7.9-8.3 kHz (kick),
22.9 kHz (snare): within the recordings' spread.
Note: `sonic_1/z80.asm` is not the ROM's driver - its loop plays only the high nibble.

### [x] 1.9 Regression
`tests/test_rom_units.py` (hand-built bytes; with the ROM: all 68 sounds vs the asm, the asm round
trip, the DAC samples).  `tests/regression.py`: `title_screen_rom`, `green_hill_zone_rom` share
their asm baselines.

### [x] 1.10 Data fixes, named labels (2026-10-03, the user's call)
- Sonic is read with the fixes ("the music will sound better"); other games may not hold these
  exact bytes, so `core/rom/fixes.py` keys them by SHA-1 and checks each one's original bytes.
  Marble Zone ($754BA, notes), Credits ($781BD, 5 bytes deleted: a splice in the decoder),
  Teleport ($791A0, header pitch).  Fixed and shipped both read as their asm on all 68; the ROM
  cases now include Marble Zone and Credits against their (fixed) baselines.
  `rom_import.py --shipped` reads both sides without.
- `write_asm` names labels by role, ROM addresses as comments: `Mus81_Loop00:  ; $74615`.

---

## Phase 2: other SMPS 68k games

### [ ] 2.1 Driver variant
One `DriverVariant`, shared with `vgz_conversion.md` 2.1 (both inputs read the same tables).
The ROM adds: pointer format (song-relative / absolute), header layout, flag table (byte → flag,
operand length), and how the song index is found.

### [ ] 2.2 Finding songs in an unknown ROM
- The FM / PSG frequency tables and the PSG envelope set, by pattern → driver family.
- An index block like `Go_`, or a heuristic header scan: plausible counts and tempo, and channel
  pointers that decode to code ending in stop or jump.
- With a rip: the VGZ lift's notes (`vgz_conversion.md` 1.3) searched as note / duration bytes.
List candidates.  Never pick one silently.

### [ ] 2.3 Variants, in order
1. Other Sonic 1 builds (rev00, prototypes) and hacks: same driver, other addresses.
2. SMPS 68k games from SMPSPlay's driver definitions, each with a VGZ.  With no disassembly,
   accept by `compare_songs(rom song, lifted rip)` plus `vgm_pitch_audit` / `vgm_compare` of
   the MOD.

### [ ] 2.4 Unknown data
A flag outside the variant's table, or a pointer outside the ROM: fail, naming the address.

---

## Phase 3: SMPS Z80 (Sonic 2, Sonic 3 & Knuckles)

Z80 bank pointers (little-endian), Saxman compression (Sonic 2), Z80 DAC tables, the tempo
overflow algorithm (shared with `vgz_conversion.md` 2.3).  Each needs a ROM + disassembly pair
(s2disasm, skdisasm) to accept against, as Phase 1 has.

---

## Open questions

- **Data fixes for other games:** Phase 2 adds a game's fixes only where its own disassembly
  (or a measured bug) names them; none are guessed from Sonic 1's.
- **Speed shoes** (`SpeedUpIndex`): out of scope.
