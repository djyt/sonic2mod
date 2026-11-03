# OutRun: SMPS Z80 on Timer A

OutRun plays its music with an SMPS Z80 driver that combines Golden Axe's layout with Sonic 1's flags,
voices and DPCM drums — and is clocked by the YM2612's Timer A at 126 Hz instead of the 60 Hz V-int.
For anyone converting its songs or a driver with its own clock.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs from Sonic 1](#how-it-differs-from-sonic-1)
- [How it plays](#how-it-plays)
- [DAC](#dac)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `OutRun (USA, Europe).md`, `GM 00004052-00`, SHA-1 `95d2055f…` |
| Driver | `smpsz80_outrun`, `core/drivers/smpsz80/outrun/` |
| Songs | 5, `$81`-`$85` |
| Configs | `configs/outrun/`: 5 minimal configs (all but Last Wave state their rip's `dac_stall`), `rips.yaml` |
| Source | no disassembly: the Z80 driver read with `z80dis` |

**From Golden Axe** ([golden_axe.md](golden_axe.md)): the sound header at its bank's start, word
pointers, `$FB` transposition, `$FC` / `$FD` / `$FE`, a handler then `inc de` dispatch.  **From Sonic
1:** a Sonic-style flag set, its voices and DPCM, a DAC track.

## Where things are

Each found by the code that reads it, or by what the 68k writes into the driver's RAM before releasing
it (`outrun/locate.py`).

| What | Where | Note |
|------|-------|------|
| Driver | ROM `$F60A8`, `$13B2` bytes → Z80 `$0000` | copied through pointers (`movea.l ($EFFF8),a0 / move.l ($EFFFC),d0`: `core/rom/z80.py`'s pointed copy) |
| RAM words | Z80 `$1C00`-`$1C07` = `0F 00 00 80 5A 02 0F 80` | written by the 68k: the songs' bank and the DAC's (bit 7 of the high byte A15, the low byte A16-A23), the sound header's address, Timer A |
| Songs bank | ROM `$F0000` at Z80 `$8000` | |
| DAC bank | ROM `$F8000` | |
| Sound header | Z80 `$8000` | `+4` music index (`$8479`, `$81`-`$85`), `+8` frequency envelopes (`$800E`), `+$A` PSG envelopes (`$80C0`, 17) |
| Flag table | Z80 `$0B25` | a table per kind of track |
| FM table | Z80 `$0AA1` | one octave: the driver splits note − `$81` into octave (the block) and semitone (`$0319`) |
| Timer A | `$25A` | 126.22 Hz NTSC |

SFX live in Z80 RAM and are not read.  **Songs** are SMPS headers: the DAC track first, FM1-FM6 in
header order (FM3 a normal channel), PSG1-PSG3.

## How it differs from Sonic 1

| | OutRun | Sonic 1 |
|---|---|---|
| Clock | Timer A overflow, 126.22 Hz | V-int, 60 Hz |
| First tempo hold | a frame (update) late (`tempo_phase` 1); 0 never holds | at frame *m* − 1 |
| FM word + detune | kept within an octave (`pitch.octave_wrap`) | added as is |
| Volume slots | a TL byte's bit 7 marks each operator the volume adds to | the algorithm's carriers |
| Frequency envelopes | `$F4 n` | none |
| DAC samples | 11, each with its own pan; DPCM or raw PCM | 3 (+ pitched timpani) |
| Pitch | `$81` plays fnum `$284` at block 0 (Sonic 1's C#0) | `$81` = C0 |

## How it plays

`outrun/variant.py`, `outrun/layout.py`.

**Clock.**  The update runs at each Timer A overflow, polled between DAC bytes: `$25A`, 126.22 Hz NTSC
(`PlaybackRules.update_clock`, `core/smps/clock.py`).  A frame is one update; the MOD's BPM and every
render step follow that rate.  `$EA` / `$EB` (Timer A set / add) are refused: no song uses them.

**Tempo.**  TempoWait's (`$0997`); 0 never holds.  The first hold comes a frame late (`tempo_phase` 1):
the queue loads a song after the update's tempo check.

**Flags** (operands from each handler, a table per kind of track):

| Byte | OutRun | Music uses |
|------|--------|-----------|
| `$E0` | pan, AMS / FMS (`$B4`); a PSG track writes nothing | 120 |
| `$E1` `$E2` `$E7` | detune, communication byte, no attack | |
| `$E5` | FM set volume (absolute) | |
| `$E6` / `$EC` | FM / PSG alter volume: the other kind of track ignores it | 355 / 2 |
| `$EF n` | voice n; bit 7: from another song's table (refused) | 175 |
| `$E3` `$F2` | stop (`$E3` after a call) | |
| `$F6`-`$F9` | jump, loop, call, return | |
| `$F3` `$F5` | PSG noise form, PSG voice (an FM track ignores `$F5`) | 7, 129 |
| `$F4 n` | frequency envelope n (0 none) | 2 |
| `$FA` `$FB` | divider, transposition (add) | —, 90 |
| `$FF n` | 8 sub-commands; `$FF $02 s` plays sound s: dropped (Last Wave's waves) | 1 |
| `$E4` `$E8` `$E9` `$EA` `$EB` `$ED` `$EE` `$F0` `$F1` `$FC`-`$FE` | refused: no song | — |

**A flag after a note ends it.**  `$BB $E7 $3C` is `$BB` at the last duration, then a tie of `$3C`
(`$0341`).  The shared walk reads every driver so.

**FM word.**  The word plus the detune is kept within an octave (`$0563`): at or under fnum `$283` a
block down, over `$508` a block up (`TrackRules.pitch.octave_wrap`), in playback and renders.

**Voices.**  25 bytes, Sonic 1's (`SMPS_VOICE`).  A TL byte's bit 7 marks an operator the track volume
is added to (`$0D17`): the carriers, and in voice `$32` (Passing Breeze, Step on Beat) OP1 too
(`SmpsVoice.volume_slots`).

**Envelopes.**
- PSG: `$80` restart, `$81` hold, `$83` mute, any other command jumps to the step its next byte names.
- Frequency (`smpsz80/pitch_envelopes.py`): `$80` restart, `$81` `$83` hold, `$82 n` jump, `$84 n`
  scale, from `$85` negative; the scaled offset kept whole.
- A tie plays both on from the attack (`$0391`: `ties.restarts_envelope` False).

**Pitch.**  `$81` plays `$284` at block 0, C#0 in Sonic 1's table.  Names stay SMPS names, as Space
Harrier II's: samples render at the table's word.

## DAC

`outrun/dac.py`.

- 13 entries at the DAC bank's start (read from Z80 `$01DC`), 11 with bytes: pointer, length, rate,
  flags.  Flag bit 0 = raw 8-bit PCM, else Sonic 1's DPCM; bits 4-7 are written to `$B6` as the sample
  starts: each sample's own pan (`PlaybackRules.dac_pans`).  The songs play DPCM alone.
- A rest lets the sample play on.
- **Rate:** the play loop's count — DPCM `160 + 13 × rate` cycles an output, PCM `171 + 13 × rate` —
  less the update's stall.  The update runs inside the loop, 33-40 % of the time by song, so a config
  states its rip's [`dac_stall`](../glossary.md#dac_stall):

```yaml
# configs/outrun/81_magical_sound_shower.yaml
dac_stall: 0.349       # the update's share of the DAC's time, from the rip
```

```bash
python tools/vgm/vgm_analyze.py "reference/vgz/outrun/<rip>.vgz" --dac-stall    # core/audit/dac_stall.py
```

## How it converts

```bash
python convert.py configs/outrun
python tools/vgm/vgm_frames.py --all --configs configs/outrun
python tools/vgm/vgm_pitch_audit.py --all --configs configs/outrun --convert
```

The rips' frames are cut after each Timer A reset write (`FrameCut.TIMER_A`: the reset ends each
update).  They run 0.21-0.30 % slow, so the audits retime them onto the driver's clock first
(`core.vgm.retimed`, `core.audit.rip_on_song_clock`).

**Against the rips** (all 5 songs):
- `vgm_frames`: every note of all 5 on its frame (FM pitch, level, voice; PSG level).
- `vgm_pitch_audit`, `vgm_compare`: every FM and PSG tone note right (13,825).
- `measure_volumes`: one pass, 69 volumes; left are DAC drums at the 64 ceiling and few-note
  instruments.
- Rip faults: PSG3's tone writes are unlogged (the noise follows tone 3, muted); Last Wave's FM5 holds
  the SFX it starts; their DAC plays 1-2 % fast against the count, as Sonic 1's rips do.

## Known limits

- `$FF $02` (a sound played from a song, Last Wave's waves) is dropped: each song converts alone.
- Refused, as no song uses them: modulation (`$F0` `$F1`), note fill (`$E8`), Timer A changes, raw YM
  writes, slide and raw-frequency modes, FM3 special mode, a voice from another song's table.
- The SFX are not converted.
