# Space Harrier II: early SMPS Z80

Space Harrier II plays its music with an early SMPS Z80 driver, Golden Axe's ancestor: songs as track
lists instead of SMPS headers, voices as register lists, per-frame pitch envelopes, and a drum track
whose byte is a bitmask of FM and PSG drums.  For anyone converting its songs or another game of this
driver generation.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs from Sonic 1](#how-it-differs-from-sonic-1)
- [How it plays](#how-it-plays)
- [Drums](#drums)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `Space Harrier II (World).md`, `GM 00004002-01`, SHA-1 `db4285e4…` |
| Driver | `smpsz80_sh2`, `core/drivers/smpsz80/sh2/` |
| Songs | 25, `$81`-`$99`: FM1, FM2, FM4-FM6, the drum track on FM3 and (all but `$97`) its PSG half on PSG3 |
| Configs | `configs/space_harrier_2/`: 20 minimal configs, `81_harrier_saga_4ch.yaml` (a merged Amiga build), `rips.yaml` |
| Source | no disassembly: the Z80 driver read with `z80dis` |

**Its family.**  Golden Axe ([golden_axe.md](golden_axe.md)) inherits its note grammar, `$EF` voice,
`$F0` set volume, `$F2` stop, `$F6`-`$F9`, `$FB`, `$FC` slide mode, `$FD` raw frequency and a drum
track on FM3, with little code shared (117 bytes).  Its register / value voice loader
(`cp $83 / ret z / cp $B0`) is also in Super Thunder Blade, Zoom!, Alex Kidd in the Enchanted Castle,
Altered Beast, Super Hang-On and Rambo III; the flag dispatch matches byte for byte only in Alex Kidd's.

## Where things are

Each table found by the code that reads it (`sh2/locate.py`).

| What | Where | Note |
|------|-------|------|
| Driver | ROM `$16A6C`, `$14F6` bytes → Z80 `$0000` | the 68k copies one of three programs (the driver, two PCM voice players) and reads each byte back (`core/rom/z80.py`'s verified copy); `smpsz80/program.py` `driver_ram` picks the one with an FM table |
| Bank | ROM `$10000` at Z80 `$8000` | the run of 9 writes to `$6000` that maps inside the ROM (the other maps `$C00000`: the PSG is written through the bank window) |
| Music index | Z80 `$87FC` | songs `$81`-`$99`: as many as the tempo table has bytes |
| Tempo table | Z80 `$87E3` | a byte per song |
| Pitch envelopes | Z80 `$863B` | |
| Voices | Z80 `$883A` | |
| FM table | Z80 `$098F` | |
| PSG table | Z80 `$08FD` | starts at A2 |
| Flag jump table | Z80 `$05B7` | |
| Pan animations | Z80 `$0482` | |
| Track RAM | Z80 `$1839` | `$30` a track: 7 music, 4 SFX |

Pointers are little-endian.  SFX (in Z80 RAM, from `$A0`) are not read.

**Songs** (`sh2/header.py`): no SMPS header.  A count byte, then a 9-byte record per track, copied into
track RAM:

| Offset | Field |
|--------|-------|
| +0 | flags: bit 7 plays, bit 6 pan animation |
| +1 | channel: FM `$00`-`$02`, `$04`-`$06`; PSG `$80` `$A0` `$C0` |
| +2 | divider |
| +3 | pointer (word) |
| +5 | transposition, signed |
| +6 | pitch envelope (0: none) |
| +7 | voice: not loaded as the song starts (a track sets its own with `$EF`) |
| +8 | volume, added to the carriers' TL |

The slot, not the channel byte, decides how a track plays: slot 3 is the drum track (FM3), slot 7 its
PSG half on PSG3, reading the drum track's own bytes.  No song plays PSG tone music.

## How it differs from Sonic 1

| | Space Harrier II | Sonic 1 |
|---|---|---|
| Song header | a track list (9-byte records); tempo in a driver table | SMPS header |
| Durations | byte × divider kept in a byte: past 255 wraps, 0 lasts 256 ticks | the product as is |
| Tempo | every n frames each track holds one; 0 never holds | TempoWait |
| Voices | (register, value) pairs, `$83` ends | 25 bytes |
| FM pitch | its own table, block in bits 11-13: each note 11 semitones 26 cents above its SMPS name | real pitch at its name |
| Pitch envelopes | per-frame offsets, from each read | none |
| Legato | `$EE`: each read ties, a rest still keys off | `smpsNoAttack` per note |
| Pan | a pan animation steps C L C R each read | static |
| Drums | a bitmask: two FM units on FM3 (special mode) + a PSG part | DAC samples |

## How it plays

`sh2/variant.py`.

**Durations.**  Byte × divider, built by adds into a byte (`$0449`), which the track counts a frame up
to (`$024E`): 0 — a note before any duration byte — lasts 256 ticks, and a product past 255 wraps
(`TrackRules.byte_durations`).  `$98`'s FM4 opens on three such notes, and on its loop the jump leaves
18: where a jump leaves another duration than the label's opening notes took, the walk walks a second
pass and loops on it (`core/smps/code.py`).

**Tempo.**  Every n frames each music track holds one (`$1802`): Sonic 1's TempoWait at phase 0.
Tempo 0 never holds — every song but `$8C`, `$93`, `$95`, `$99`, which use 3.

**Flags:**

| Byte | Space Harrier II | Music uses |
|------|------------------|------------|
| `$E0`-`$E6`, `$F3` | PSG noise form (operand ORed with `$E0`) | drums |
| `$E7` | LFO (`$22`) | — |
| `$E8` | a driver byte (`$1808`: stops the music when set) | — |
| `$E9` | the song the 68k queues next: dropped, each song converts alone | 40 |
| `$EA` `$EB` `$FF` | FMS, AMS, pan (`$B4`) | 52 (`$FF`) |
| `$EC` `$ED` | FM3 special mode on / off | drums |
| `$EE` | legato on (1) / off: each read ties, a rest still keys off | 96 |
| `$EF` | voice | 618 |
| `$F0` `$F1` | set volume | 8 |
| `$F2` | stop | |
| `$F4` | pitch envelope | 12 |
| `$F5` | PSG envelope (PSG tracks) | drums |
| `$F6`-`$F9` | jump, loop, call, return | |
| `$FA` | divider | drums |
| `$FB` | transposition (add) | 60 |
| `$FC` `$FD` | slide mode, raw frequency mode | drums |
| `$FE` | alter volume | 68 |

The dispatch steps one byte past a handler's return, so a flag with no operand of its own still skips
one.

**Voices** (`sh2/voices.py`): (register, value) pairs of channel-0 registers, `$83` ends; the carriers
take the volume.  Voices 18, 20 and 22 are patches over the voice before, not voices: an `$EF` naming
one is refused (none does).

**Pitch.**  Its own FM table, the block in bits 11-13: each note sounds 11 semitones and 26 cents above
its SMPS name (`$81` near B0).  Samples render at the table's word, so the MOD is right; note names in
configs and reports stay SMPS names, and the pitch audit adds the word's cents
(`core/plan/instrument_plan.py`).

**Pitch envelopes** (`sh2/envelopes.py`, `core/smps/pitch_envelope.py`): per-frame offsets from each
read; `$80` restart, `$84 n` scale += n, `$85 n` jump to step n, `$81`-`$83` hold.  Envelopes 1-3
scoop, then loop a vibrato that deepens each pass; 49 tracks start with one.  Baked: the detune plan
keys a sample on (detune, envelope), rendered with the envelope stepped a frame at a time, never
looped.

**Pan animation.**  Record flag bit 6: each read steps the first list (C L C R, again and again: the
driver sets no other).  Converted as `PanStep`, written as `8xx` where no other effect is.

## Drums

`sh2/drums.py`; the drum track's byte is a bitmask, read by the FM half (Z80 `$0CC0`) and the PSG half
(`$0DFE`).

- **FM:** two units on FM3 in special mode, each an operator pair with its own frequency sweep.  Unit A
  (OP1-OP2): bit 3 plays record `$0BFA`, else bit 0 `$0C0E`; bit 5 the next record.  Unit B (OP3-OP4):
  bit 2, record `$0BF0`.  Each frame op X's low byte += its step, op Y's high byte = op X's low byte +
  step.
- **PSG:** bit 3 a noise-only part; bits 0-2 tone 3 at a sweep with noise `$E7` (white) clocked by it,
  tone 3 two attenuation steps under the noise unless bit 3.
- **Two bugs played as heard.**  The drums sound so on every play and the songs were written against
  them — the exception to "don't simulate bugs".  Unit B reads `$0BF0` where the code meant `$0BE6`
  without bit 5 (which no song sets); the op-Y step lands in the high byte, so OP2 / OP4 jump a block on
  frame 2.
- **Rendering:** a hit is rendered whole as a one-shot, its PSG part mixed in at
  `fm_synthesis.drum_psg_db` (−5.7 dB, code default and shipped: measured on 19 rips, VGMPlay mixing the
  SN76496 at half the YM2612's volume).  A drum keying only part of its voice starts its derived volume
  that far below (`drum81`: OP1-OP2 alone).

## How it converts

```bash
python convert.py configs/space_harrier_2/81_harrier_saga.yaml
python convert.py configs/space_harrier_2/81_harrier_saga_4ch.yaml --merged   # the 4-channel Amiga build
python tools/vgm/vgm_frames.py --all --configs configs/space_harrier_2
python tools/vgm/vgm_frames.py --all --glitches --configs configs/space_harrier_2
```

**Against the rips** (`rips.yaml`: 20 rips; `$82`-`$86`, the stage theme's other sections, have none):
- `vgm_frames`: every FM attack's pitch, level and voice on its rip's frame in 19 songs (`$81`'s five
  lost V-ints and `$96`'s and `$98`'s one, [glitches](../glossary.md#glitch), are logged in `rips.yaml` and
  undone); every drum hit's keys,
  OP4 word and PSG part.
- `vgm_pitch_audit`: all 20 songs, 7,424 FM notes right.
- `measure_volumes`: one pass, 147 volumes.
- `vgm_lift` misreads the tempo-3 songs' holds as tempo changes: `vgm_frames` is the yardstick here.

## Known limits

- **`$98`'s FM4 voice** differs in OP1's SSG-EG: the `$F2` stop (`$06D3`) silences a track through its
  RR / TL list offset by `channel and 7`, so FM4's lands an operator slot high (`$90` = `$FF`), and no
  voice clears it.  Which song plays with it depends on what stopped before, so it is not converted.
- **Volumes left:** drums with too few hits to measure; voices whose quieter channels play a sample
  rendered at a louder one's level (An Epitaph's FM6, ~7 dB).
- **Pitch envelopes are approximated:** a sample rendered at one pitch carries the envelope scaled to
  the notes it plays elsewhere; a tie under legato does not restart it.
- The SFX and the PCM voice players are not converted.
