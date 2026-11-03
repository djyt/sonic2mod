# Golden Axe: SMPS Z80 Type 0 FM

Golden Axe plays its music with an early SMPS Z80 driver: no DAC music, no PSG music, its drums FM
programs on FM3.  Sonic Retro classes it as Type 1 FM's early form, with Flicky and Fighting Masters.
For anyone converting its songs or reading another FM-drum Z80 game.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs from Sonic 1](#how-it-differs-from-sonic-1)
- [Drums](#drums)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `Golden Axe (World) (Rev A).md`, `GM 00054018-01`, SHA-1 `2ce17105…` |
| Driver | `smpsz80_type0fm`, `core/drivers/smpsz80/type0fm/` |
| Songs | 15, `$81`-`$8F` (`$8A`, `$8F` are stubs): every one 6 FM, 0 PSG |
| Configs | `configs/golden_axe/`: 13 minimal configs, each with an `hq` variant; `rips.yaml` |
| Source | no disassembly: the Z80 driver read with `z80dis` (`pip install z80dis`) |

## Where things are

Each found by the code that reads it (`type0fm/locate.py`), not by Golden Axe's addresses.

| What | Where | Note |
|------|-------|------|
| Driver | ROM `$1D2F0` → Z80 `$0000`, `$DF8` bytes | copied uncompressed by the 68k (`$34DC`); found by its FM table; `core/rom/z80.py` reads the copy loop |
| Bank | ROM `$18000`-`$1FFFF` at Z80 `$8000` | set once by the 68k (`$1C00` = 1, `$1C01` = `$80`); the one bank starting with a sound header |
| Sound header | Z80 `$8000` | words: `+0` priorities, `+4` music index, `+6` SFX index, `+8` pitch envelopes |
| Music index | Z80 `$8069` | 15 songs |
| SFX index | Z80 `$8087` | 42, `$90`-`$B9` (not converted) |
| Flag table | Z80 `$0B65` | |
| FM table | Z80 `$07D9` | indexed from note `$80` |
| PSG table | Z80 `$074D` | no song uses the PSG |
| Drums | Z80 `$0899` (trigger), `$096B` (records), `$0987` (voices) | [Drums](#drums) |

Every song, voice and pointer lives in the bank; pointers are absolute Z80 addresses, little-endian.
The sound queue's commands `$E0`-`$E3` are fade, stop, one not identified, and the SEGA voice (DAC, Z80
`$0F00`).  Songs use Sonic 1's
header fields.  SFX use Sonic 1's layout, tracks on FM3-FM6 and the PSG, one shared voice bank.

## How it differs from Sonic 1

| | Golden Axe | Sonic 1 |
|---|---|---|
| Track order (Z80 `$0501`) | the drum track, FM1, FM2, FM4, FM5, FM6 | DAC, FM1-FM5, PSG1-3 |
| Drums | FM programs on FM3 | DAC samples |
| Voice | 26 bytes: `$B0`, `$B4` (pan, AMS, FMS: setting the voice pans the track), then TL, DT/MUL, RS/AR, AM/D1R, D2R, SL/RR | 25 bytes, no pan |
| FM pitch | its own table: `$81` = C0 as Sonic 1, every note above 6-18 cents flat (mostly −12) | Sonic 1's |
| Tempo 0 | never holds (Death Adder) | — |
| First tempo hold | a frame late (`tempo_phase` 1) | at frame *m* − 1 |
| Long notes | keyed off after 256 frames without an attacking read (`key_run_out` 256) | ring on |
| Flag with no handler | skips one operand | — |

**Flags** (Z80 `$0B65`).  A handler starts at the first operand and the driver steps one byte past it,
so a flag with no handler of its own skips one operand.

| Byte | Type 0 FM | Sonic 1 | Music uses |
|------|-----------|---------|------------|
| `$E0`-`$E4`, `$E8`-`$EE`, `$F1`, `$F3`-`$F5`, `$FA`, `$FF` | no handler, 1 operand (no pan, fill, modulation) | various | — |
| `$E5` `$E6` | alter volume | tempo divider / alter volume | — |
| `$E7` | no attack (FM only) | same | 256 |
| `$EF` | set voice | same | 131 |
| `$F0` | **set volume**, absolute (`CoordFlag.SET_VOL`) | modulation | 16 |
| `$F2` | stop | same | 25 |
| `$F6` `$F7` `$F8` | jump, loop, call | same | 65, 199, 94 |
| `$F9` | return | FM1 release rate | 34 |
| `$FB` | transposition (add) | — | 2 |
| `$FC` | slide mode on / off | — | drum programs only |
| `$FD` | raw-frequency mode (note = fnum word) | — | — |
| `$FE` | FM3 special mode, 4 operands | — | — |

**Volume** adds to the carriers' TL, as Sonic 1's.

**The first tempo hold comes a frame late.**  The counter is loaded as the song starts, after that
frame's tempo check (`$06CA` runs before `$043A`), so `rules.tempo_phase` is 1.  Evidence: FM1's
key-ons land on the rips' frames — Sutakora 128 of 128 (1 of 128 at Sonic 1's phase), Showdown 245/245,
Wilderness 235/235.  The update runs at V-int on NTSC; on PAL, at YM timer B `$CB` (62.8 Hz).

**Run-out.**  A note keyed 256 frames without an attacking read is keyed off (`$00E8`: the fill
counter, never set, wraps; a tie's read neither resets nor counts it; `core/smps/run_out.py`).  The rips
key Death Adder's, The Battle's and Conclusion's long FM1 notes off 258-260 frames after the attack.

**Frequency writes:** every FM channel's, every frame.  A rip therefore shows no read at a tie: the
[lift](../glossary.md#lift) merges ties that change nothing, and the pitch audit segments at pitch changes.

## Drums

The drum track's note `$8n` starts FM drum n on FM3 (bits 4-6 would start a PSG drum: no song plays
one).  `type0fm/drums.py`.

The [drum track](../glossary.md#drum-track) plays no samples: each hit runs a short FM program.

- **A drum** is a 6-byte record (`$096B`: program pointer, transpose, volume, envelope, voice index into
  `$0987`) and a program: track bytecode in the driver, run at divider 1.  The record's volume adds to
  its voice's carrier TL; the drum track's own volume is 0 in every song (not read).
- **Programs** use slide mode (`$FC 01`: note, slide, a skipped byte, duration; a signed fnum step a
  frame, the octave wrapping at fnum `$27E` / `$4FE`) and `$E7` tie chains of 1-2 frame notes.
- Drum 9 is a rest (a hit only stops the drum before it); drum 10 never stops (it runs into voice
  data) and is cut where it stops being heard.
- **Rendering:** each program runs frame by frame and is rendered whole as a one-shot sample at
  `samples.drum_root` (`configs/settings.yaml`: C3); a silent drum's hits become `C00`.

Evidence: run frame by frame, every program matches the rips' FM3 exactly — all 4,477 hits in the 11
rips with drums, at some TempoWait phase.

## How it converts

```bash
python convert.py configs/golden_axe/81_wilderness.yaml
python convert.py configs/golden_axe/81_wilderness.yaml --variant hq
python tools/vgm/vgm_lift.py configs/golden_axe/89_the_battle.yaml --skip FM3
python tools/vgm/vgm_frames.py configs/golden_axe/89_the_battle.yaml
```

The `hq` variant sets `max_window` to the narrowest
[window](../glossary.md#window) (the note range one sample covers) that fits the song in 31 sample
slots without a merge — 1-4 semitones, against 9 — with its own measured `sample_list`.

**Against the rips** (`configs/golden_axe/rips.yaml`):
- `vgm_pitch_audit`: all 13 songs, 14,345 notes right.
- `vgm_lift --skip FM3`: FM matches exactly on The Battle, Battle Field, Turtle Village 2, Showdown,
  Conclusion, Sutakora and Game Over; the rest differ at the first note and the loop.  Path of Fiend
  matches to 38 s, where the rip loses a V-int (a [glitch](../glossary.md#glitch)) (frame 2304: the burst before re-keys every channel and
  runs past its frame); `rips.yaml` logs it and the rip tools undo it
  ([../guide/verifying.md](../guide/verifying.md)).  Death Adder: one tie to another pitch the lift
  cannot see.
- `vgm_frames`: every attacking FM note's pitch, level, voice and pan as recorded, all 13.
- Levels (`measure_volumes`): within 2 dB, except Turtle Village 1's FM1 (below).

## Known limits

- **Turtle Village 1 FM1** plays 16 dB quiet: 240 four-tick arpeggio notes on voice `$02` (carrier AR
  16).  The chip re-keys before the envelope falls, so the level builds; every MOD note restarts its
  sample from silence.  No volume reaches it.
- **Battle Field** loops its drum track at 6,096 ticks, its FM at 6,720: the game drifts them apart;
  the MOD replays both from its loop (the converter warns).
- The SFX and the SEGA voice are not converted.
