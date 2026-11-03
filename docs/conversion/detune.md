# Detune variants and the hardware LFO

How a raw frequency offset (`smpsDetune` / `smpsAlterNote`), channel 3's per-operator offsets and the
YM2612's shared LFO reach a MOD, which can only retune a whole sample.  For anyone wondering why an
instrument has extra sample slots, or why a detuned double sounds right without `finetune`.

## Why a detune needs its own sample

`smpsDetune` / `smpsAlterNote` ($E1) adds a raw offset to the frequency word (`FMUpdateFreq`), so its
interval depends on the note:

```
$03 on C   (FNUM 644):   1200 · log2(647 / 644)   = +8 cents
$03 on A#  (FNUM 1148):  1200 · log2(1151 / 1148) = +4.5 cents
```

It is not semitones and never affects range lookup ([notes.md](notes.md)).  A MOD retunes only a whole
sample, in 12.5-cent finetune steps, so `core/plan/detune.py` renders every detune an instrument plays
into a sample of its own: a [**detune variant**](../glossary.md#detune-variant).  settings.yaml
`fm_synthesis.detune_variants: false` turns this off: detuned notes then play the instrument's own sample.

> **Pitfall:** Never stand in for a detune with `finetune: 1`: that moves every note of the slot,
> detuned or not.

## How variants are planned

- The detune most of an instrument's notes play at is its [**own detune**](../glossary.md#own-detune): its slot's sample is rendered
  with it (Sonic 1's Title Screen FM5 double, `smpsAlterNote $03`, takes no extra slot).
- Every other detune is a **variant** in a free slot, the most played first, sharing the instrument's
  entry, level and `sample_list` volume / finetune.
- One with no free slot plays the instrument's own sample (`detune_no_slot`: Sonic 1's Credits), an
  `E1x` / `E2x` on the note's row moving it the rest of the way where that row's effect is still free.
- A sample carries its detune's interval at the pitch it is rendered at.  Where that is more than a
  finetune step off the interval its notes need (Streets of Rage's +195: +336 c on F#, +266 c on A#), a
  variant is rendered in its notes' commonest pitch class (the same `root`, another
  [`synth_shift`](../glossary.md#synth_shift)), and notes the detune moves more than 25 c from their
  sample's interval get a variant per pitch class.
- `resolve_note` routes a note to its variant, so every pass sees it.  The plan is made on the song as
  parsed, before the loop extension, as the audit tools make it (`prepare_instruments`).
- A merged chip composite renders each layer at its own track's detune.

Example, Green Hill (`configs/sonic_1/02_green_hill_zone.yaml`): FM5 doubles FM4 on instrument 7 with
`smpsAlterNote $02`; its 16 detuned notes play a variant rendered at +2 FNUM in a free slot.

### Ties after a detune change

A **tie** after a detune change (Sonic 1's Scrap Brain FM4 scoop: `smpsAlterNote $EC`, a note,
`smpsAlterNote $00`, `smpsNoAttack`, duration) is re-written by the driver at the new detune.  The MOD
note keeps its sample, so the tie's row gets `E1x` / `E2x` by the period difference (only on a row of
its own with a free slot; `convert.py --verbose` counts them).

### PSG

PSG tones work the same way: an own detune and variants, one variant per detune (no pitch classes).

- The detune is the offset the driver adds to the tone divider (with Streets of Rage 2's shift by the
  note's octave applied).
- A divider is a period, so the same offset is a different interval at another pitch.  A sample
  rendered at another pitch carries the offset scaled by its divider over its notes' commonest
  (`_psg_offset`).  Example: +2 on B7's divider 29 is −115 cents; at A3 (divider 508) the same interval
  is +35.
- A note the detune moves by more than half a semitone sounds as the note nearest its divider
  (`PsgTable.nearest`, the driver's wrap or clamp included: `PitchRules.divider`): it routes and plays as
  that note, with the rest of the offset as its detune.  Streets of Rage's `$8F` PSG1 ramps B7's 29 by
  2–3 a note down to A2, then wraps to the top: it plays across the PSG's samples, not as 750 variants
  of one B7 sample.

### Channel 3's special mode

Streets of Rage's `$F7` puts FM channel 3 in special mode: each operator at the note's word plus its own
offset.  It becomes a copy of the voice that carries the offsets (`SmpsVoice.fnum_offsets`, made in the
walk as `$FA` patches are), rendered on channel 3 with register `$27` = `$40`.  Its offsets count in the
intervals above (OP4's, the channel's own A2 / A6: what a rip reads as its pitch), so its notes get a
variant per pitch class at the instrument's own detune too (Moon Beach's FM3 drums: OP4 +100 FNUM is
+254 c on C, +193 c on F).

## Hardware LFO

The YM2612 has one LFO: register `$22` sets its frequency for every channel, a channel's `$B4` how far
it moves that channel (FMS its pitch, AMS the level of operators with AM on).  Streets of Rage's
`$FC f p a` writes both, so a note plays at the frequency the last `$FC` of any track wrote (`$88`: FM5's
`$FC 2 3 2` slows FM2's and FM4's vibrato; `$8B`: FM4's `$FC 0 0 0` drops FM5's to 3.8 Hz).

`core/smps/lfo.py`, a pass over the walked song, gives each attacking FM note a copy of its voice under
its LFO (`SmpsVoice.lfo`); the sample is rendered with it (`$B4` and `$22`).  A MOD sample cannot follow
the chip's free-running phase:

- under AMS the render starts a quarter cycle in, the level swing at its middle (as a note starting
  anywhere hears it on average; at step 0 it is at its quietest);
- under FMS alone it starts at step 0, the pitch at its centre.

Its sustain loop spans whole LFO cycles (`core/audio/loops.py`, `cycle`: [samples.md](samples.md#sustain-loops-samplessustain_loops)).
