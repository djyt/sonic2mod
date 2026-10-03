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

`core/rom/`, beside `core/vgm/` (source → rom → smps).  `tools/rom_import.py ROM --compare reference/smps_drivers/sonic_1`:
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
Note: `reference/smps_drivers/sonic_1/z80.asm` is not the ROM's driver - its loop plays only the high nibble.

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

First target: **Michael Jackson's Moonwalker (World) (Rev A)** - `GM 00004028-01`, SHA-1
`70d9b760…`, SMPS 68k Type 1a (Sonic 1 is Type 1b).  No disassembly: the facts below come from its
driver, read with capstone (`$60000`-`$61700`), and `reference/sega_retro/`.

### Probe (2026-10-03)

- **Found:** music index `$600A4`, 23 songs `$81`-`$97` (Sega Retro's Rev 01 column, exactly).  It
  sits in the driver's `Go_` block at `$60000` (Sonic 1's order: priorities `$60100`, special SFX
  `$67C78` (4), music `$600A4`, SFX `$67BC4` (49), speed-up, PSG_Index `$60020` (6 envelopes)).
  `locate_sounds` misses it: it looks for Sonic 1's 96-entry FM table and its `$80`-terminated
  envelopes.
- **Same as Sonic 1:** song headers (relative pointers, DAC first, 6-byte PSG entries), 25-byte
  voices in the same register order, TempoWait tempo (`$60BC0`), durations x divider, PSG pitch
  (`note - $81 + transpose`, 69-entry table `$611F6`), FM pitch values (a one-octave table at
  `$61024`, block = `(note - $80 + transpose) / 12`: Sonic 1's 96 words are exactly that table
  per block - only notes past the top wrap differently), modulation (`$F0`), note fill, jump /
  loop / call pointers.
- **Flags** (jump table `$61290`, 31 entries; `$FF` runs into the `$E0` handler): with Type 1a's
  operand lengths all 23 songs decode into contiguous code; with Sonic 1's, 8 fail.

  | byte | Type 1a (Moonwalker) | Sonic 1 | used |
  |------|----------------------|---------|------|
  | `$E3` | sets a global flag (`$FC` clears it), 0 operands | return | no |
  | `$E4` | pan animation: mode, 0 → off, else table, index, limit, speed (5 bytes) | fade | 6 |
  | `$E5` | alter volume: FM byte, PSG byte | channel tempo divider | no |
  | `$E9` | LFO: register `$22`, AMS/FMS | transposition | no |
  | `$EA` `$ED` `$EE` | detune (as `$E1`), 1 operand | tempo / push / stop special | no |
  | `$EB` | queue a sound (all 3 uses: `$DD`, a voice sample) | tempo divider, all tracks | 5 |
  | `$F9` | return | FM1 release rate | 75 |
  | `$FA` | channel tempo divider | - | 4 |
  | `$FB` | transposition | - | 63 |
  | `$FC` | clears `$E3`'s flag | - | no |
  | `$FD` | SSG-EG, 4 operands | - | no |
  | `$FE` | FM3 special mode, 8 operands | - | no |
- **PSG envelopes** are the ROM's, not `driver_tables`': six, ending `$83` (hold - Sonic 1's
  `$80`); `$80` restarts the envelope, `$85 nn` jumps to index nn; envelope 6 has no terminator
  and runs into 5.  Songs use 1, 4, 5.
- **DAC:** the Z80 driver is copied uncompressed (code `$6166A`, 525 bytes; samples `$61876`, 7442
  bytes, to Z80 `$0210`); 12-byte table entries; 5 samples in Z80 RAM, IDs 6 on streamed from ROM
  banks (the voice samples, `$1FF8` set by the 68k); two delta arrays (`$1A` drums, `$2A` voice:
  `-3` for `-2`).  The 68k remaps notes: `$88`-`$8F` play sample `$85` at a pitch from `$602FA`,
  `$90`+ sample `$82` at a patched rate.  Songs use `$81`-`$84`, `$88`, `$8A`, `$8C`, `$90`, `$91`.
- **Tempos:** modifiers 3 … 32 and 255 (the title jingle: a hold every 255 frames).

### Work before Moonwalker converts

### [x] 2.1 Driver variant (done 2026-10-03, `core/rom/drivers.py`)
`RomDriver`: each flag byte's `FlagSpec` (effect, no-attack, return, stop, jump, loop, call,
drop, refuse; operand count, `more_if_set` for `$E4`).  `SmpsDriver.TYPE1A` =
`smps68k_type1a`.  A config's `driver:` is now optional: a ROM's is detected, an asm or a VGM log
is Sonic 1's.  The FM pitch rule, envelopes and DAC scheme join the variant in 2.4-2.5.

### [x] 2.2 Locate (done 2026-10-03)
`locate_sounds` finds the `Go_` block by its tables' shape (music / SFX / special entries at
plausible headers, PSG_Index at envelopes: steps 0-$1F up to a command byte - Sonic 1's PSG2 steps
to $10) behind the FM octave check: Sonic 1 $71990, Moonwalker $60000, one candidate each, 0.1 s.
`detect.py`: pinned by SHA-1 (Sonic 1 rev01, Moonwalker Rev A), else the one driver every song
decodes with; none or two stop with what each attempt hit.  Sonic 1 under Type 1a fails at $81's
`$E9` (LFO, refused); Moonwalker under Sonic 1 at `$FF`.

### [x] 2.3 Decoder flags (done 2026-10-03)
Type 1a's table as the probe found it.  All 23 songs read through the shared walk; the seven
that loop in the VGZ pack loop on every channel; dropped: 5 pan animations, 3 queued sounds
(`SongCode.dropped`, listed by `rom_import.py`).  Refused (none in the songs): `$E3` / `$FC`
(a global flag nothing here explains), `$E5` (two-byte volume), `$E9` LFO, `$FD` SSG-EG, `$FE` FM3
special.  3 of the 49 SFX use them (`$A3` LFO, `$BD` / `$BE` the flag): listed as not read.

### [x] 2.4 PSG envelopes from the song (done 2026-10-03)
`PsgEnvelope(steps, loop_to)`: `loop_to` None holds the last step, else the steps from it repeat.
`SmpsSong.psg_envelopes` (Sonic 1's `SONIC1_ENVELOPES` for an asm song or a lift; a ROM's read from
its PSG_Index by `core/rom/envelopes.py`, each driver's commands in `RomDriver.envelope_commands`).
The PSG generator (`psg_envelopes=`) and the converter's noise sustain read the song's table; a
looping envelope is unrolled over the render.  Sonic 1's ROM envelopes equal the transcription;
Moonwalker's six differ (03, 06), none loop, envelope 6 runs on into 5.  Baselines byte-identical.
`played_song` still compares envelopes by name.

### [x] 2.5 DAC samples (done 2026-10-03)
`dac.py` per driver.  Moonwalker: the 68k's two copy loops fill Z80 RAM, 12-byte entries, 5 RAM
samples; `$88`-`$8F` copies of `$85` at the pitch table `$602FA`, `$90`-`$97` copies of `$82` at
`$16` (`$90`: `$1E`).  Decodes checked against the rips' PCM banks: `$82`-`$84` whole, `$81`'s
body whole in 11 rips (the logs drop its 90 bytes of leading silence), `$85` for 1024 bytes (cut by
the next hit).  Rates: the play loop counts 415 + 26 (pitch - 1) cycles a byte, but the rips run
slower; fitted per sample 235.6 + 13.96 (pitch - 1) to `$84` (15190 Hz) and `$82` (6770), it
predicts `$81` at 10740 Hz against the rip's 10765.  Sonic 1 keeps its count (the rips: kick
7.9-8.3 kHz against 8201, snare 22.9 kHz against 23784).
DAC names come from the driver (`RomDriver.dac_names`: Sonic 1's `dKick` …, Type 1a `dac81` …
for every byte to `$DF`), so the walk makes every Moonwalker DAC hit a DAC note.  The ROM-streamed
voice samples (from `$86`) are not read: 2.7 drops them.

### [ ] 2.6 Pan animation (`$E4`)
No MOD panning: dropped, counted in the report.  If a stereo build ever exists, it is a pan
table walk.

### [ ] 2.7 Song-triggered sounds (`$EB`)
Bad, Round Clear and a dance queue voice sample `$DD`.  Dropped, with a report line (the user's
rule: what is not part of the music goes).  The VGZ pack agrees: each dance is ripped with and
without voice, so the game lays the voice over the music.

### [x] 2.8 Minimal config (the user's choice; done 2026-10-03, `core/plan/derive.py`)
The YAML holds what the ROM cannot say and the musical choices; everything else is derived when
converting, so it improves with the converter.  `convert.py --show-config` prints the config used,
`--write-config` freezes it for hand-tuning.  The Sonic configs stay as they are (measured, pinned).

    name: "Smooth Criminal"
    input_file: "input/roms/Michael Jackson's Moonwalker (World) (Rev A).md"
    rom_song: "$81"
    # overrides only: an override replaces the item it names (one voice's entries), not the section

| User | Derived |
|------|---------|
| `input_file`, `rom_song`, `name` | `driver:` (SHA-1, else the 2.2 structure scan; ambiguous → stop, list candidates) |
| `merge:` / `merge_patterns:` / `merge_drop:` | `range_space: chip` (ROM songs transpose: `$FB` x63) |
| per-entry `loop_drift_db`, `dither`, `treble_shelf_db` | `ticks_per_row`, `target_speed`, BPM (durations, tempo) |
| `region` (PAL only) | `channels:` (header order; silent ones out) |
| | `voice_map` / `psg_voice_map` / `psg_map` (chip ranges per voice and envelope, 3-octave splits, roots; `make_credits_config.py`'s logic generalised) |
| | `dac_samples` (2.5; `mod_note` the period nearest each rate) |
| | `mod_instrument` slots (in order, 31; overflow reported) |
| | `sample_list` volumes (the level law, then `vgm_compare --write-volumes` against the VGZ) |
| | `mod_pattern_breaks` (the loop on a pattern's row 0) |

Also: `analyze.py --rom-song`; `rom_import.py` shows the variant, tables, songs, SFX.

Done as planned, with these choices:
- A config without `channels:` is minimal (the Sonic configs state everything; `range_space` unstated
  could not be the test).  `load_config` / `complete_config` complete one for `convert.py` and every
  tool (`vgm_pitch_audit`, `vgm_compare`, `merge_survey`, `fold_csv`, `config_to_chip_space`,
  `analyze.py --config`); `convert.py --show-config`, `--write-config`; the report lists the derived
  sections and the dropped flags.
- Roots: a window's lowest pitch at E1, the first MOD note above the sample audit's 5 kHz low-rate
  line (C1 flagged every sample "low rate 4144").  Names as the disassembly spells them (F5, Bb2).
- Volumes: `starting_volume`, analyze.py's skeleton law moved to core (samples are peak-normalised,
  so the volume carries the modal TL / attenuation: 76 x gain at TL 0, PSG 16 at attenuation 0).
  `vgm_compare --write-volumes` adds stated rows to a minimal config.
- DAC: a sample at the note and finetune nearest its rate; its pitched copies at the nearest note at
  that finetune.  Smooth Criminal's kick: lowest peak 72.7 Hz in rip and MOD (70-71 Hz without).
- Smooth Criminal: one `--write-volumes` pass put every instrument within 1 dB of the rip (most
  within 0.4).  `configs/moonwalker/`: 21 minimal configs (every music index sound but `$8B`, a
  copy of `$87`), all convert; output in `output/moonwalker/`.  Not in regression (the user's call).
- analyze.py, untouched for a while: Sonic DAC file names fixed for other names (`dac81` gave
  `ac81.raw`); detune and smpsModSet no longer "partial" (both rendered exactly), pan is (no MOD pan,
  -3 dB); a ROM song's skeleton is the derived config.

Pitch audit (`vgm_pitch_audit`, by rip name): Smooth Criminal 1022/1022, Beat It 786/786, Another
Part of Me 759/761, Billie Jean 885/885, Bad 663/663, Mr. Big 1099/1099, Boss 112/112, Title 20/20,
Game Over 5/5, Round Clear 67/77, Dance Attack 4-6 clean; Dance Attack 1-3 and 7-12 mostly "wrong":
the rips' game order is not `$8C`-`$97` (and the pack may be Rev 00, whose `$95`-`$97` are the
Thriller dances) - 2.9's matcher.

### [ ] 2.9 Yardstick
Volumes (2026-10-03): `measure_volumes.py --rips configs/moonwalker/rips.yaml` (the pairs the pitch
audit confirmed: 13 of 21) - 68 volumes in 11 songs, one write pass; Smooth Criminal clean after,
the rest's residuals at the 64 ceiling, one- or two-note instruments, or Beat It scaled down so its
loudest fits (PSG at volume 1, noise +6 dB with nowhere lower).  Dance Attack 1-3 and 7-12 keep the
starting volumes until the matcher pairs them.

Open findings from 2.8:
- Round Clear: all FM channels a whole 1-3 semitones off at 0.30 s and 2.30 s; Another Part of Me
  -100 c on FM1 and FM4 at 53.68 s.  Simultaneous whole-semitone shifts: a transposition or legato
  timing rule of Type 1a's that differs from Sonic 1's.
- Smooth Criminal: 6-10 key-ons a channel unmatched at the same times on FM1/3/4/5 (40.42, 48.42,
  52.42 s ...), as many MOD-only: a timing rule again?
- The jingles (Title, Game Over) hold their last FM note 14-22 s: past the 10 s sample cap.

`reference/vgz/moonwalker/`: vgmrips' complete set, 40 rips (12 dances twice, with and without
voice).  By name: $81-$85 Smooth Criminal ... Bad (rips 03, 06, 09, 11, 13), $88 Round Clear (04),
$89 Mr. Big (15), $8A Boss (07), $87 / $8B Game Over (28), $8C-$97 Dance Attack 1-12 (16-27).
Title Screen (01) is likely $86; Round 1-5 Start (02, 05, 08, 10, 12) and Final Boss Demo (14) are
not in the music index - find them (the 49 SFX?).  A matcher pairs each rip with its sound by
`align_songs` / `compare_songs` on the lifted onsets, not by name.  Then `vgm_pitch_audit`,
`vgm_compare`, the volumes; SMPSPlay (Type 1a) to listen against.

### [ ] 2.10 Unknown data
A flag outside the variant's table, or a pointer outside the ROM: fail, naming the address.

### Later
Other Sonic 1 builds and hacks (same driver, other addresses); Golden Axe II (Type 1b);
`input/roms/` also holds Golden Axe (Rev A) and OutRun (SMPS Z80 Type 1 DAC: Phase 3).

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
