# Moonwalker: SMPS 68k Type 1a

Michael Jackson's Moonwalker plays its music with SMPS 68k Type 1a, an older sibling of Sonic 1's
driver: the same song headers, voices, tempo and pitch, with its flags renumbered and a different
volume law.  For anyone converting its songs or comparing it with Sonic 1.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs from Sonic 1](#how-it-differs-from-sonic-1)
- [DAC](#dac)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `Michael Jackson's Moonwalker (World) (Rev A).md`, `GM 00004028-01`, SHA-1 `70d9b760…` |
| Driver | `smps68k_type1a`, `core/drivers/smps68k/type1a/` |
| Songs | 23, `$81`-`$97` (Sega Retro's Rev 01 column) |
| Configs | `configs/moonwalker/`: 22 minimal configs, `rips.yaml` |
| Source | no disassembly: the driver (`$60000`-`$61700`) read with capstone, and `reference/sega_retro/` |

## Where things are

Sonic 1's `Go_` block layout, at the driver's start (`$60000`), found by
`core/drivers/smps68k/locate.py` as Sonic 1's is.

| What | ROM | Note |
|------|-----|------|
| `Go_` block | `$60000` | priorities `$60100`, special SFX `$67C78` (4), music, SFX `$67BC4` (49), speed-up, `PSG_Index` |
| Music index | `$600A4` | 23 songs |
| PSG envelopes | `$60020` | 6 |
| Flag jump table | `$61290` | 31 entries, `$E0`-`$FE` |
| FM table | `$61024` | one octave |
| PSG table | `$611F6` | 69 entries |
| TempoWait | `$60BC0` | |
| Voice load | `$61402` | carrier TLs written at `$61478` (slot mask `$614C0`) |
| DAC player | code `$6166A`, samples `$61876` | copied uncompressed to Z80 `$0210` |

## How it differs from Sonic 1

**The same:** song headers (relative pointers, DAC first, 6-byte PSG entries), 25-byte voices in the
same register order, [TempoWait](../glossary.md#tempowait), durations × divider, PSG pitch (`note − $81 + transpose`), modulation
(`$F0`), note fill, jump / loop / call.  FM pitch reads a one-octave table with block =
`(note − $80 + transpose) / 12`: the same words as Sonic 1's 96, only notes past the top wrap
differently.  The converter uses Sonic 1's FM and PSG tables (compared: equal).

**Flags** — the bytes that differ (`type1a/variant.py`; "used" counts the songs' uses):

| Byte | Type 1a | Sonic 1 | Used | Converted as |
|------|---------|---------|------|--------------|
| `$E3` | sets a global flag (`$FC` clears it), no operands | return | no | refused |
| `$E4` | pan animation: mode, 0 → off, else table, index, limit, speed | fade | 6 | dropped |
| `$E5` | alter volume: FM byte, PSG byte | channel tempo divider | no | refused |
| `$E9` | LFO: register `$22`, AMS/FMS | transposition | no | refused |
| `$EA` `$ED` `$EE` | detune (as `$E1`) | tempo / clear push / stop special | no | detune |
| `$EB` | queue a sound (`$DD`, a voice sample) | tempo divider, all tracks | 5 | dropped |
| `$F9` | return | FM1 release rate | 75 | return |
| `$FA` | channel tempo divider | — | 4 | divider |
| `$FB` | transposition | — | 63 | transposition |
| `$FC` | clears `$E3`'s flag | — | no | refused |
| `$FD` | SSG-EG, 4 operands | — | no | refused |
| `$FE` | FM3 special mode, 8 operands | — | no | refused |
| `$FF` | past the table: runs into `$E0`'s handler | — | | pan |

"Refused" means the reader stops on it: no song holds one.

**Carrier TL = the track volume**, not Sonic 1's voice TL + volume.  Loading a voice writes its 24
operator registers, then writes each carrier's TL from the track's volume (`$9(a3)`) alone, as `$E6`
does after changing it.  The voice's own carrier TLs never play (`VoiceLayout.carrier_tl=False`); a
volume with bit 7 set writes none.  Evidence: an algorithm 5 voice's third carrier would play 27 TL
quieter than every rip shows (`tools/vgm/vgm_frames.py`).

**PSG envelopes:** the ROM's six, ending `$83` (hold); `$80` restarts, `$85 nn` jumps to step nn.
Envelope 6 has no terminator and runs into 5.

**Tempos:** modifiers 3-32, and 255 (the title jingle).

## DAC

A Z80 sample player copied uncompressed with its samples: 12-byte table entries at Z80 `$0210`, 5
samples in Z80 RAM (from `$86` they stream from ROM banks: the voice samples, not read).

The 68k remaps notes before the Z80 sees them, so every note byte names a DAC sound (`dac81` …):

| Note | Plays | At pitch |
|------|-------|----------|
| `$88`-`$8F` | `$85` | from the table before the write: `05 08 0E 12 16 1A 20 30` |
| `$90`-`$97` | `$82` | patched: `$16`, `$90` at `$1E` |

The patched pitch stays in Z80 RAM; each converted sample plays at the pitch its own byte sets.

**Rate.**  The loop at `$00B6` counts `415 + 26 × (pitch − 1)` cycles a byte, but the rips play slower.
The converter uses a fit to them: `235.6 + 13.96 × (pitch − 1)` cycles per sample, from `$84` (pitch 1,
15,190 Hz) and `$82` (`$16`, 6,770 Hz); it predicts `$81` (`$08`) at 10,740 Hz where Smooth Criminal's
rip has 10,765.  The count misses the loop's YM busy-wait and the Z80's timing as the rips' emulator
runs it.

## How it converts

Minimal configs, everything derived from the ROM:

```bash
python convert.py configs/moonwalker/81_smooth_criminal.yaml --show-config
python tools/vgm/vgm_compare.py configs/moonwalker/81_smooth_criminal.yaml "reference/vgz/moonwalker/03 - Smooth Criminal.vgz" --write-volumes
python tools/vgm/vgm_lift.py --all --configs configs/moonwalker
```

**Against the rips:**
- `vgm_lift` (the rip read back into a song: [lift](../glossary.md#lift)): every song's FM plays as recorded, except FM5's first note, a tick late in 4 rips.
- `vgm_frames`: every FM note's pitch, level and voice as recorded, except that note and the logs'
  loop and end frames.

## Known limits

- `rips.yaml` pairs 13 songs; Dance Attack 1-3 and 7-12 have none (the rip pack names them in game
  order, not `$8C`-`$97` order).
- The voice samples (`$86` up) and the pan animation are not converted.
- The SFX are not read.
