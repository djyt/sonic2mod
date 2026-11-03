# Effects

What each SMPS coordination flag becomes in the MOD, which effect wins a cell's single slot, and the
rules for the effects that need arithmetic: note delays (`EDx`), note fill cuts, vibrato (`4xy`) and
legato.  For anyone reading a converted MOD's effect column or changing `core/convert/channel_writer/`.
ProTracker's own effects: [../reference/mod_effects.md](../reference/mod_effects.md); the flags'
driver behaviour: [../drivers/sonic_1.md](../drivers/sonic_1.md).

**On this page**

- [SMPS → MOD mapping](#smps--mod-mapping)
- [Effect priority](#effect-priority)
- [Notes that start between rows (`EDx`)](#notes-that-start-between-rows-edx)
- [Note fill (`smpsNoteFill`)](#note-fill-smpsnotefill)
- [Vibrato (`smpsModSet` → `4xy`)](#vibrato-smpsmodset--4xy)
- [Legato (`smpsNoAttack` before a note)](#legato-smpsnoattack-before-a-note)

## SMPS → MOD mapping

| SMPS | Byte | MOD | Rule |
|------|------|-----|------|
| `smpsAlterVol` | $E6 | `Cxx` | FM TL offset, 0.75 dB/step; `Cxx` only where a note's level differs from its instrument's baked level ([levels.md](levels.md)) |
| `smpsPSGAlterVol` | $EC | `Cxx` | PSG attenuation, 2 dB/step; same rule |
| `smpsPan` | $E0 | (level) | MOD pan is per channel; a hard-panned FM note counts `fm_pan_law_db` (3 dB) quieter |
| `smpsModSet` / `smpsModOn` / `smpsModOff` | $F0 / $F1 / $F4 | `4xy`; a slow one `1xx` / `2xx` / `E1x` / `E2x` | [§ Vibrato](#vibrato-smpsmodset--4xy) |
| `smpsNoteFill` | $E8 | `ECx` / `C00` / release slide | [§ Note fill](#note-fill-smpsnotefill) |
| `smpsNoAttack` | $E7 | note-on or `3FF` | [§ Legato](#legato-smpsnoattack-before-a-note) |
| `smpsDetune` / `smpsAlterNote` | $E1 | (sample), `E1x` / `E2x` | [detune.md](detune.md) |
| Streets of Rage's `$F7` (FM3) / `$FC` | | (sample) | channel 3's special mode, the hardware LFO: voice copies ([detune.md](detune.md)) |
| `smpsChangeTransposition` | $E9 | (placement) | adds to the driver transpose ([notes.md](notes.md)) |
| `smpsSetvoice` | $EF | (routing) | picks the `voice_map` list ([notes.md](notes.md)) |
| `smpsPSGform` / `smpsPSGvoice` | $F3 / $F5 | (routing) | `psg_map` / `psg_voice_map` ([../synthesis/psg.md](../synthesis/psg.md)) |
| `smpsSetTempoMod` | $EA | `Fxx` | [timing.md § Mid-song tempo changes](timing.md#mid-song-tempo-changes-smpssettempomod-ea) |
| `smpsSetTempoDiv` / `smpsChanTempoDiv` | $EB / $E5 | (re-timing) | [timing.md § Global duration divider](timing.md#global-duration-divider-smpssettempodiv-eb) |
| `smpsJump` | $F6 | `Bxx` (+ `Dxx`) | the song's loop ([song_structure.md](song_structure.md)); a short loop is replayed |
| `smpsStop`, `smpsFade`, `smpsStopSpecial` | $F2, $E4, $EE | key-off, `D00` | [song_structure.md § A song that stops](song_structure.md#a-song-that-stops) |
| `smpsLoop` / `smpsCall` | $F7 / $F8 | — | unrolled / inlined by the parser ([../drivers/smps_asm.md](../drivers/smps_asm.md)) |
| `smpsNop`, `smpsClearPush`, `smpsMaxRelRate` | $E2, $ED, $F9 | — | ignored |
| DAC note | | the drum | `dac_samples`: its instrument at its `mod_note` |

## Effect priority

A MOD cell holds one effect, but a note may want several: a delay, a volume, vibrato, a cut.  The
converter decides by what is lost if an effect gives way.  On a note's rows:

1. **Attack row** (the row the note starts on): `EDx` (delay), `3FF` (legato) and `9xx` (sample offset)
   take the slot, since without them the note itself is wrong.  A `Cxx` due there moves to the note's
   first later row with a free slot: one row at the instrument's own level is a small error.  Without
   those three, `Cxx` > `4xy` > an in-row cut `ECx`, except that an attack-row `ECx` displaces `4xy` (a
   note that short has no audible vibrato).  A cut inside an attack row that needs `Cxx` moves to the
   next row's start (`C00`).
2. **Later rows:** `Cxx` (moved) > `4xy` > `ECx` / `C00` (fill) > a
   [sliding loop](../glossary.md#sliding-loop)'s fall (`A0y` / `EBx`, or `6xy` on a row whose `4xy` the
   note set earlier: [samples.md](samples.md#sliding-loops-loop_decay-slide)).
3. **After the note:** the [release slide](../glossary.md#release-slide) (`A0y` rows) takes only free
   slots and stops at the next note-on's row ([samples.md](samples.md#release-slides)).

A volume change on a vibrato row drops that row's vibrato; `smpsAlterVol` is sparse, so this is rarely
heard.

## Notes that start between rows (`EDx`)

A note whose tick is off the row grid goes on the row it starts in, delayed by `x` MOD ticks with `EDx`
(`Cells.note_cell`, `core/convert/channel_writer/cells.py`), instead of being rounded up to half a row
early or late.

**The delay is measured in frames.**  Ticks are unevenly spaced
([timing.md § Frames versus ticks](timing.md#frames-versus-ticks)), so:

```
frames = frame of the note's tick − frame of its row's first tick
x      = round(frames × target_speed × ticks_per_frame / ticks_per_row)      (x ≥ speed: next row, no delay)
```

Worked: Green Hill (*m* = 3, `ticks_per_row: 2`, speed 3), a note on tick 3:

```
row 1 starts at tick 2  → frame 3          the note's tick 3 → frame 4
frames = 1               (16.7 ms, not the 25 ms an average tick lasts)
x      = round(1 × 3 × 2/3 / 2) = round(1.0) = 1      → ED1 on row 1
```

- **It needs the slot.**  If a cut (a fill, or a PSG note's end) falls inside the attack row, the cut
  keeps the slot and the note is rounded instead.  A due `Cxx` gives way when the note lasts two rows or more (it moves to a later
  row); an attack-row `4xy` gives way (later rows carry the vibrato); a rest's `C00` there is
  overwritten (the new note ends the old one).
- **Two note-ons never share a cell.**  When the row already holds the channel's previous note-on (a
  one-tick grace note), the later one takes the next row, undelayed.
- DAC notes carry no other effect, so they always get their delay (unless a `9xx` takes the slot).

## Note fill (`smpsNoteFill`)

[**Note fill**](../glossary.md#note-fill) cuts a note a fixed number of frames after its attack, leaving the rest of its duration
silent (a staccato).  The fill counts frames; on the tick timeline it is `fill × ticks_per_frame`.

- The cut is placed to the MOD tick: `ECx` inside a row, `C00` on a row boundary (`ECx` cannot reach
  past its own row).
- Nothing is written when the fill outlasts the note (`fill × ticks_per_frame ≥ duration`: the duration
  expires first) or lands on the next event's row.
- A fill equal to the duration byte **does** fire when *m* > 1: the note lasts `duration × m/(m−1)` frames.
- With release slides on, a fill on an FM note starts the release slide instead of a cut (its sub-row
  position is given up).
- PSG notes with no fill are cut at the end of their ring, `smpsNoAttack` continuations included
  (`ChannelWriter._collect_ring_ticks`).

Example: Green Hill (2/3 tick a frame, speed 3, 2 ticks a row), fill 3 on a note at row 10 tick 0:
3 frames = 2 ticks = one row → `C00` on row 11.  Fill 2: 1.33 ticks = 2 MOD ticks into row 10 →
`EC2` on row 10.

## Vibrato (`smpsModSet` → `4xy`)

`smpsModSet wait, speed, delta, steps` adds `delta` to the note's frequency word every `speed` frames,
reversing after `steps` steps ([../drivers/sonic_1.md](../drivers/sonic_1.md)).  That gives:

- a steady **cycle** of `2 · speed · (steps + 1)` frames: Sonic 1 spends a step at each turn that adds
  nothing.  A driver whose turn moves too (Streets of Rage) has `2 · speed · steps`
  (`TrackRules.modulation.turn_pause`; its first leg `modulation.first_leg_short`);
- a **swing** of `delta · steps / 2` units (divided by 2^`pitch.word_shift` where a driver's PSG shifts
  the sum right: Streets of Rage's, by 4) of the note's own frequency word: the YM2612 FNUM of its pitch class in the song's table
  (Sonic 1's: 644 for C … 1148 for A#, B 606 in the block above, so a B swings as wide as a C) or the PSG
  divider.  The same `smpsModSet` is deeper in cents on C than on A#.

ProTracker advances its vibrato position by `x` on each of a row's `speed − 1` ticks and wraps at 64.
`VibratoSpeed.speed` / `vibrato_depth` (`core/convert/vibrato.py`):

```
x     = 64 · ticks_per_row / ((target_speed − 1) · cycle_frames · ticks_per_frame_at(tick))
swing = period · (delta · steps / 2) / frequency_word         (Amiga periods, per note)
y     = the depth whose peak in the player is nearest the swing (_VIBRATO_PEAK)
```

`ticks_per_row` here is the timeline's, the tempo divider included.

### Worked example

Green Hill FM1, `smpsModSet $0D, $01, $07, $04` (wait 13, speed 1, delta 7, steps 4); *m* = 3,
`ticks_per_row: 2`, `target_speed: 3`:

```
cycle  = 2 · 1 · (4 + 1)                  = 10 frames  (6 Hz at 60 fps)
x      = 64 · 2 / ((3 − 1) · 10 · 2/3)    = 128 / 13.3 = 9.6  → A
swing  = delta · steps / 2                = 14 FNUM units

a C at MOD C-2 (period 428, FNUM 644):    428 · 14 / 644  = 9.3 periods   → y 5 (FT2 peak 9.75, PT2 9)   4A5
an A# at MOD A#1 (period 480, FNUM 1148): 480 · 14 / 1148 = 5.9 periods  → y 3 (FT2 peak 5.75, PT2 5)   4A3
```

### Rules

- The peak depends on the player (settings.yaml `player`; `merged_player` for a `--merged` build;
  `convert.py --player` overrides both): PT2 peaks at `2y − 1` whole periods, FT2 at `2y − ¼`
  ([settings.md](../guide/settings.md#top-level) has the defaults).
- A swing under 0.7 periods writes no vibrato (the smallest depth would overshoot threefold).  An `x`
  past 15 plays at 15 and is reported (`vibrato_rate_limit`).
- A row carries `4xy` when modulation runs for at least half of it, the attack row included, from the
  `smpsModSet` wait (frames) on; the continuation stops at the release slide.
- A per-entry `vibrato: XY` override wins; no shipped config needs one.
- Region-independent (both clocks scale with fps).  What remains is the 4-bit grid: one step of `x` is
  0.4–0.6 Hz, one step of `y` 10–30 cents.

### A cycle too slow for `4x1`

When `x` rounds to 0 (Streets of Rage's 251 steps, Moonwalker's 255) the modulation is a sweep the note
never sees turn, not a vibrato: `4x1` would wobble it ±the whole swing many times too fast.  Each row
with a free slot slides instead to the chip's pitch at its end (`modulation_offset`: the wait, `delta`
every `speed` frames, a turn after half the steps):

- `1xx` / `2xx` on the row's later ticks, `E1x` / `E2x` under a period a tick;
- what the MOD reached is carried, so a taken row is made up on the next;
- the modulation runs from the attack or the `smpsModSet` / `smpsModOn` after it (a tie runs it on:
  Streets of Rage's Dilapidated Town FM2 chains), and a tie re-struck for its level
  (`legato: retrigger`) slides back from the note's period on its next row.

## Legato (`smpsNoAttack` before a note)

The driver writes the new frequency and skips the key-on: the envelope carries on at the new pitch
(Sonic 1's Green Hill one-tick grace notes bending into chords; Drowning's FM3 slide line).  A MOD
note-on re-triggers its sample.  settings.yaml `legato` chooses how such a note is written:

| `legato` | Writes |
|----------|--------|
| `retrigger` (default, shipped) | every no-attack note as a plain note-on |
| `strict` | `3FF` (full-speed tone portamento, no re-trigger), with the rules below |
| `loose` | `3FF` on the target's own instrument, always |

`retrigger` trades a grace note heard as two attacks for never riding a decayed tail: Green Hill FM1
holds an E for 2.8 s, then a no-attack C; the hardware plays it at a fresh note's level, a `3FF` there
is 26 dB down.

`strict` follows the rule that in FT2 clone and ProTracker a portamento never changes the sample:

- a target in **another range** of the voice (another instrument, rendered for another octave) is
  written on the sounding instrument, its MOD note moved by the chip-pitch difference (a target that
  would leave C1–B3 is re-triggered on its own instrument);
- after an **`smpsSetvoice`** the note is re-triggered (the hardware rewrites the operators under the
  running envelope);
- with **nothing sounding** on the channel yet it is a note-on (Drowning's FM3 is no-attack from its
  first note).

`loose` sounds right only in a player that swaps the sample on an instrument number (OpenMPT).

> **Note:** A `3FF` takes the attack row's slot: no `EDx` (the note is rounded), a due `Cxx` moves a
> row.  Under `strict` / `loose` the sustain need counts a legato note as the same ring, and
> `tools/mod/mod_lint.py` flags a `3xx` with nothing playable under it.
