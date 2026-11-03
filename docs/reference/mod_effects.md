# ProTracker effect reference

The effect commands of a ProTracker MOD, and for each one whether and how sonic2mod writes it.  A reference
for reading a converted MOD in a tracker or changing the channel writers; why each SMPS feature maps to the
effect it does is in [`../conversion/effects.md`](../conversion/effects.md).

A cell holds one effect: a command nibble and a parameter byte (`xy` = two nibbles, `xx` = the whole byte),
all hexadecimal unless stated.  Examples show a cell as tracker text: `C-3 00 047` = note C-3, no instrument
change, effect `0`, parameter `47`.

> **Note:** a MOD cell has room for one effect, so the converter ranks them
> ([effect priority](../conversion/effects.md#effect-priority)): `Cxx` > `4xy` > note cut > a loop's fall slide.

On this page:

- [Effect table](#effect-table)
- [Main effects `0`–`F`](#main-effects)
- [Extended effects `E0`–`EF`](#extended-effects)

---

## Effect table

"Used" names the code that writes it; "—" means sonic2mod never writes it.

| Cmd | Name | Parameter | Used by sonic2mod for |
|---|---|---|---|
| `0xy` | Arpeggio | semitones x, y | — |
| `1xx` | Pitch slide up | speed | a vibrato too slow for `4xy`; a glide's row slides |
| `2xx` | Pitch slide down | speed | as `1xx` |
| `3xx` | Tone portamento | speed | `3FF`: legato (`smpsNoAttack`) |
| `4xy` | Vibrato | rate x, depth y | `smpsModSet` |
| `5xy` | Portamento + volume slide | up x, down y | — |
| `6xy` | Vibrato + volume slide | up x, down y | a release / loop fall on a row carrying the note's `4xy` |
| `7xy` | Tremolo | rate x, depth y | — |
| `8xx` | (unused in ProTracker; panning in FT2) | pan | a driver's pan animation steps |
| `9xx` | Sample offset | offset / 256 | a sound inside a merged build's sample bank |
| `Axy` | Volume slide | up x, down y | `A0y`: release slides, sliding sustain loops |
| `Bxx` | Position jump | position | the song's loop; pattern breaks' retargeted jumps |
| `Cxx` | Set volume | 00–40 | levels that differ from the sample's; `C00` cuts and rests; volume envelopes |
| `Dxx` | Pattern break | row (decimal) | pattern breaks; a loop to a mid-pattern row; `D00` where a song stops |
| `E0x` | LED filter | 0 on, else off | — |
| `E1x` | Fine slide up | periods | detune on ties and unplaced detunes; slides under a period a tick |
| `E2x` | Fine slide down | periods | as `E1x` |
| `E3x` | Glissando | 0 / 1 | — |
| `E4x` | Vibrato waveform | waveform | — |
| `E5x` | Set finetune | finetune | — (finetune is set in the sample header) |
| `E6x` | Pattern loop | count | — |
| `E7x` | Tremolo waveform | waveform | — |
| `E8x` | Karplus-Strong / trigger | any | the end trigger on a stopping song's last row |
| `E9x` | Retrigger | tick | — |
| `EAx` | Fine volume up | units | — |
| `EBx` | Fine volume down | units | a fall smaller than one `A01` row |
| `ECx` | Note cut | tick | a cut inside a row (note fill, key-off) |
| `EDx` | Note delay | tick | a note that starts between rows |
| `EEx` | Pattern delay | rows | — |
| `EFx` | Invert loop | speed | — |
| `Fxx` | Set speed / BPM | 01–1F speed, 20–FF BPM | row-0 speed and BPM; mid-song tempo changes |

---

## Main effects

### `0xy` Arpeggio

Cycles the pitch every tick between the note, note + x and note + y semitones: a chord on one channel.

```
C-3 00 047   C major (C, E, G)
C-3 00 037   C minor (C, D#, G)
```

sonic2mod: not written.

### `1xx` Pitch slide up

Lowers the period by `xx` on every tick of the row but the first.  Stops at B-3 (period 113).  Speed
changes alter how far a slide goes.

```
C-3 00 103   slide up 3 a tick
```

sonic2mod: a modulation whose cycle is too slow for `4xy` (a sweep) slides each row to the chip's pitch at
the row's end (`core/convert/vibrato.py`, `curve_slides`); so do a driver's glides and portamento (Time
Trax's, `channel_writer/pitch_curve.py`).

### `2xx` Pitch slide down

As `1xx`, raising the period.  Stops at C-1 (period 856).

```
C-3 00 203   slide down 3 a tick
```

sonic2mod: as `1xx`.

### `3xx` Tone portamento

Slides from the sounding note to the cell's note at `xx` periods a tick, without restarting the sample.
`300` continues the last slide.

```
A-2 00 000   play a note
C-3 00 305   slide to C-3 at speed 5
```

sonic2mod: `3FF` (fast enough to land within a tick) on a note played without a new attack
(`smpsNoAttack`), when settings `legato` is `strict` or `loose` (`channel_writer/writer.py`).  A
portamento never changes the sample in ProTracker or FT2, so `strict` re-triggers where the target needs
another one.  [Legato](../conversion/effects.md#legato-smpsnoattack-before-a-note).

### `4xy` Vibrato

Oscillates the pitch: `x` is the rate (how far the waveform advances each tick, wrapping at 64), `y` the
depth.  `400` continues; a new `x` or `y` changes only that half.

```
C-3 00 481   rate 8, depth 1
```

sonic2mod: `smpsModSet` / `smpsModOn` (`channel_writer/modulation.py`, arithmetic in
`core/convert/vibrato.py`).  The depth is chosen for the target player (settings `player` /
`merged_player`): PT2 peaks at `2y − 1` periods, FT2 at `2y − ¼`.
[Vibrato](../conversion/effects.md#vibrato-smpsmodset--4xy).

### `5xy` Tone portamento + volume slide

Continues the last `3xx` and slides the volume (as `Axy`).

```
C-3 00 503   portamento on, volume down 3
C-3 00 540   portamento on, volume up 4
```

sonic2mod: not written.

### `6xy` Vibrato + volume slide

Continues the last `4xy` and slides the volume (as `Axy`).

```
C-3 00 605   vibrato on, volume down 5
C-3 00 640   vibrato on, volume up 4
```

sonic2mod: a release slide or a sliding sustain loop's fall on a row whose `4xy` the note set earlier, so
the vibrato goes on (`channel_writer/fades.py`).

### `7xy` Tremolo

As `4xy`, oscillating the volume.  `700` continues.

```
C-3 00 794   rate 9, depth 4
```

sonic2mod: not written.

### `8xx` (unused) / panning

Unused by ProTracker.  FT2 and most later trackers read it as panning: `00` left, `80` centre, `FF` right.

sonic2mod: a driver's pan animation (Space Harrier II's track flag), one `8xx` per pan step, only on rows
with a free slot (`channel_writer/writer.py`).  A MOD channel's pan is otherwise fixed.

### `9xx` Sample offset

Starts the note `xx × 256` bytes into the sample.  In the ProTracker replayer, a sample longer than
65 534 bytes plays nothing with it.

```
C-3 00 923   start at byte $2300
```

sonic2mod: in the [merged build](../guide/amiga_builds.md), several sounds share one slot as a sample
[bank](../glossary.md#bank); `9xx` picks the sound (`core/merge/banks.py`, `channel_writer/writer.py`).

### `Axy` Volume slide

Slides the volume up by `x` or down by `y` on every tick of the row but the first.  One of `x` / `y` is 0.
The slower the speed, the more each row moves.

```
C-3 00 A05   down 5 a tick
C-3 00 A40   up 4 a tick
```

sonic2mod: `A0y` only.  After a key-off it follows the chip's release (release slides); on a sustain loop
with `loop_decay: slide` it follows the voice's decay (`channel_writer/fades.py`).
[Release slides](../conversion/samples.md#release-slides).

### `Bxx` Position jump

Jumps to position `xx` (00–7F) of the order list at the end of the row; also breaks the pattern.

```
C-3 00 B01   continue at position 1
```

sonic2mod: the song's loop, on the last row (`core/convert/layout.py`, `ModLayout.loop_point`).  Pattern
breaks repack the patterns and retarget every `Bxx` past them (`core/mod/breaks.py`).
[Song structure](../conversion/song_structure.md#the-loop).

### `Cxx` Set volume

Sets the volume, `00`–`40` (64).

```
C-3 00 C10   volume 16
```

sonic2mod: a sample's volume is its instrument's commonest level; `Cxx` only where a note's level differs
(`channel_writer/levels.py`).  `C00` silences: a cut on a row boundary, a rest, a leading rest on row 0.
A driver's volume envelope (Time Trax's) is a `Cxx` per row where no sample bakes it
(`channel_writer/level_curve.py`).  [Levels](../conversion/levels.md).

### `Dxx` Pattern break

Jumps to the next position at the end of the row, starting at row `xx`.  The parameter is decimal: `D32`
is row 32.

```
C-3 00 D00   next position, row 0
C-3 00 D32   next position, row 32
```

sonic2mod: a config's `mod_pattern_breaks` (`core/mod/breaks.py`, `row_to_bcd`); with a `Bxx`, a loop
back to a row other than 0; `D00` on the row a song that stops ends on (`ModLayout.song_end`).

### `Fxx` Set speed / BPM

`01`–`1F` sets the speed (ticks a row); `20`–`FF` the BPM (32–255) under CIA timing.  `00` stops the song
in some players.  Under vblank timing every value is a speed.

```
C-3 00 F06   speed 6
C-3 00 F7D   125 BPM
```

sonic2mod: on pattern 0 row 0, the BPM (`target_bpm`) and, unless it is 6, the speed (`target_speed`), in
free slots after every channel is written; a mid-song `smpsSetTempoMod` writes the new BPM
(`core/convert/layout.py`).  [Timing](../conversion/timing.md).

---

## Extended effects

`E` takes a sub-command in the parameter's high nibble and its value in the low one.

### `E0x` LED filter

Toggles the Amiga's low-pass "LED" filter: `E00` on, any other value off.

sonic2mod: not written.

### `E1x` Fine slide up

Lowers the period by `x` once, on the row's first tick.

```
C-3 00 E11   up 1 period at the start of the note
```

sonic2mod: a tie heard at a detune its note-on did not have, and a detuned note whose detune variant
found no slot, retuned in place (`channel_writer/cells.py`, `fine_slide`); a row slide under one period a
tick (`curve_slides`).  [Detune](../conversion/detune.md).

### `E2x` Fine slide down

As `E1x`, raising the period.

```
C-3 00 E26   down 6 periods at the start of the note
```

sonic2mod: as `E1x`.

### `E3x` Glissando

With `E31`, a tone portamento moves in semitone steps; `E30` turns it off.

sonic2mod: not written.

### `E4x` Vibrato waveform

`0` sine (default), `1` ramp down, `2` square; add 4 to keep the waveform's position on a new note.

```
C-3 00 E42   square
```

sonic2mod: not written (sine).

### `E5x` Set finetune

Sets the note's finetune: `0`–`7` = 0 to +7, `8`–`F` = −8 to −1 (eighths of a semitone).

```
C-3 00 E51   finetune +1
```

sonic2mod: not written.  A sample's finetune comes from its `sample_list` entry, in the sample header.

### `E6x` Pattern loop

`E60` marks the loop start; `E6x` jumps back to it `x` times.

```
C-3 00 E60   loop start
C-3 00 E63   play the loop 3 more times
```

sonic2mod: not written.

### `E7x` Tremolo waveform

As `E4x`, for tremolo.

sonic2mod: not written.

### `E8x` Karplus-Strong / trigger

In the ProTracker tracker (not its replayers), low-pass filters a looped sample's loop, more the longer it
runs; it rewrites the sample.  Other players ignore it, and some replayers (ptplayer's `_mt_E8Trigger`)
report it to the program.

sonic2mod: with settings `end_trigger: x` (shipped `1`, code default off), `E8x` on the row a stopping
song ends ([end trigger](../conversion/song_structure.md#end-trigger-end_trigger)), so a game engine can tell the song is over (`ModLayout.song_end`).  Its row has no looped sample
sounding.

### `E9x` Retrigger

Restarts the note every `x` ticks.

```
C-3 00 F06   speed 6
C-3 00 E93   retrigger at tick 3 (twice a row)
```

sonic2mod: not written.

### `EAx` Fine volume up

Raises the volume by `x` once, on the row's first tick.

```
C-3 00 EA3   up 3
```

sonic2mod: not written.

### `EBx` Fine volume down

Lowers the volume by `x` once, on the row's first tick.

```
C-3 00 EB6   down 6
```

sonic2mod: a release or sliding-loop fall smaller than one `A01` row moves (`channel_writer/fades.py`).

### `ECx` Note cut

Sets the volume to 0 on tick `x`.

```
C-3 00 F06   speed 6
C-3 00 EC3   cut at tick 3 of 6
```

sonic2mod: a note that ends inside a row: an `smpsNoteFill` cut, a PSG note's end
(`channel_writer/writer.py`).  A cut on a row boundary is `C00`.
[Note fill](../conversion/effects.md#note-fill-smpsnotefill).

### `EDx` Note delay

Starts the cell's note on tick `x`.

```
C-3 00 F06   speed 6
C-3 00 ED3   play at tick 3 of 6
```

sonic2mod: a note whose tick falls between rows goes on its row with an `EDx`, measured in frames
(`channel_writer/cells.py`, `core/mod/timing.py`).  [Notes between rows](../conversion/effects.md#notes-that-start-between-rows-edx).

### `EEx` Pattern delay

Holds the pattern for `x` rows; effects keep running.

```
C-3 00 EE8   wait 8 rows
```

sonic2mod: not written.

### `EFx` Invert loop

Inverts a short looped sample byte by byte at speed `x`; `EF0` stops.  It rewrites the sample.

sonic2mod: not written.
