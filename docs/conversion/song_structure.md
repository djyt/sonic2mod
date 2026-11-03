# Song start, loop and end

How a song's beginning, its loop and its end are laid out in the MOD: replaying short channel loops
so every track reaches the song's end, silencing leading rests, the `Bxx` / `Dxx` loop jump, a
stopping song's `D00`, and pattern breaks.  For anyone whose MOD loops or ends at the wrong place.

**On this page**

- [Loop extension](#loop-extension)
- [Leading rests](#leading-rests)
- [The loop](#the-loop)
- [A song that stops](#a-song-that-stops)
- [Pattern breaks (`mod_pattern_breaks`)](#pattern-breaks-mod_pattern_breaks)

## Loop extension

SMPS channels loop independently: a hi-hat track may repeat a one-bar `smpsJump` loop while the melody
runs for minutes.  A MOD has one song position for all columns, so [**loop extension**](../glossary.md#loop-extension) replays each
short loop's body until the song's last tick (`prepare_song`, `core/smps/song_prep.py`).

- The body is the events **after the jump label** (`SmpsChannel.loop_event_index`), not every event at
  the label's tick: Sonic 1's Spring Yard PSG3 has `smpsPSGAlterVol $FF` just before its label, which
  would otherwise repeat each pass and walk the hi-hat to full volume.
- Tracks that loop at other lengths are in step again only after their periods' least common multiple
  (Streets of Rage's `$8F`: 2304, 1728 and 4608 frames, LCM 13824), so every track is replayed to the
  last jump target plus that period and the MOD loops back to the target.
- A loop's period is the shortest its body repeats at (Green Hill's drums: 1024 ticks, a 512-tick bar
  twice); one under a quarter of the longest is a texture and sets none (its hi-hat).
- A period past four times the longest loop is not unrolled: the song ends as before and `loop_drift`
  names the tracks out of step after the MOD's loop (Streets of Rage's Stealthy Steps: PSG3's loop
  period 5173 against 5120).

`convert.py --verbose` lists every loop extension.

## Leading rests

A [leading rest](../glossary.md#leading-rest) is silence at a channel's start, before its first note.  A
MOD that loops back to position 0 would otherwise ring the column's last note on through it.  So a
channel whose first event is a rest gets `C00` at pattern 0 row 0 (`ModLayout.leading_rests`, after
every channel is converted).  An effect already in that cell moves to a free row-0 cell; with none free
the `C00` is dropped, warned (`rest_no_slot`) only when the loop does return to row 0.  The row-0 `Fxx`
are placed after this ([timing.md § Tempo commands on row 0](timing.md#tempo-commands-on-row-0)).

> **Note:** Rests on the drum track write nothing anywhere in the song: the sample plays out, as on the
> chip, and an FM drum (Golden Axe's) rings on; only a silent drum's hit stops it
> (`ChannelWriter._stop_ringing`).  A driver whose rest stops the sample says so
> (`TrackRules.rest_cuts`: Streets of Rage's rests and gates play its empty `$85`), and its rests
> write `C00`.

## The loop

`ModLayout.loop_point` (`core/convert/layout.py`), after the pattern breaks:

| Part | Where |
|------|-------|
| `Bxx` row | the row of the song's end tick (`round(end / tpr) − 1`, shifted past the breaks), on the first column in use with a free effect slot; none free: `loop_no_slot` warns and column 0's effect is overwritten |
| `Bxx` target | `round(loop_target_tick / tpr)` shifted past the breaks → the pattern holding it |
| Mid-pattern target | a `Dxx` (row in BCD) on a free column **to the right** of the `Bxx`: ProTracker reads a row left to right, and a `Bxx` after a `Dxx` resets the row to 0 |
| After | patterns after the loop's are trimmed |

A loop back into another [tempo segment](../glossary.md#tempo-segment) gets its `Fxx` there too
([timing.md](timing.md#mid-song-tempo-changes-smpssettempomod-ea)).

## A song that stops

`smpsStop` (and `smpsFade`, `smpsStopSpecial`) keys the track off.  `ChannelWriter._on_stop` ends what an
FM channel still sounds there as a rest would (release slide, else `C00`); a looping channel never
stops, the DAC plays out and a PSG note is already cut.

`ModLayout.song_end` puts `D00` on the end row of a song that does not loop (none on a pattern's last
row: the next pattern is the end anyway), and the patterns after go, so the module restarts after the
key-offs, not after a pattern of silence (Sonic 1's Title Screen ends on a pattern's first row: 63 rows
of silence otherwise).

### End trigger (`end_trigger`)

With settings.yaml `end_trigger: x` (code default off, shipped `1`) that row also holds `E8x`, for a
replayer that reports it (ptplayer's `_mt_E8Trigger`, which `_mt_init` clears to 0, so x is never 0) to
tell the game the song is over.

- A row with no room for both (Sonic 1's Stage Clear: a `D00` and three `C00`s) moves them to the next
  row, after the last key-offs: the song ends a silent row later.
- FT2 clone ignores `E8x`.  The ProTracker editor itself (not its replayers) treats `E8x` as a
  Karplus-Strong filter on a looped sample sounding in its column; a stopping song's last row has none,
  so nothing is heard.

## Pattern breaks (`mod_pattern_breaks`)

A short intro followed by the loop body would share pattern 0 with the body, wasting the pattern's
remaining rows on every pass.  A [**pattern break**](../glossary.md#pattern-break) at `(pattern P, row R)` ends P at row R with
`Bxx → P+1` and repacks everything after it into full patterns from P+1 (`apply_pattern_breaks`,
`core/mod/breaks.py`):

```yaml
# configs/sonic_1/02_green_hill_zone.yaml
mod_pattern_breaks:
  - pattern: 0
    row: 31      # last intro row; the body starts at P+1 row 0
```

Every pre-break flat row (`pattern × 64 + row`) at or after `P × 64 + R + 1` moves forward by
`63 − R` rows (`shift_for_breaks`; several breaks apply in order).  Green Hill, break (0, 31): rows move
32 forward from flat row 32 on.

```
flat row 288  →  320  =  pattern 5 row 0     a loop target here: B05
flat row 287  →  319  =  pattern 4 row 63    a loop target here: B04 + D63
```

```
before                         after
pattern 0  ┌ intro rows 0-31   pattern 0  ┌ intro rows 0-31
           │ body  rows 32-63             └ B01 on row 31
pattern 1  └ body ...           pattern 1  ┌ body (was pattern 0 row 32) ...
```

The loop's `Bxx` is written after the breaks, in post-break coordinates; a `Bxx` written before would
land on a displaced row.  `row_grid: bar` derives the breaks itself
([timing.md § row_grid: bar](timing.md#row_grid-bar)).
