# Sample length, sustain loops and release slides

How long each rendered sample is, how a settled sound is cut to a loop so its length no longer depends
on the notes, and how notes then end with volume slides at the voice's release rate.  For anyone
trading file size against fidelity (`loop_drift_db`, `sustain_loops`) or hearing a buzz or a click at a
loop.  How samples are rendered: [../synthesis/fm.md](../synthesis/fm.md), [../synthesis/psg.md](../synthesis/psg.md).

**On this page**

- [Sample length versus notes](#sample-length-versus-notes)
- [Tails no note reaches](#tails-no-note-reaches-samplestrim_tails)
- [Sustain loops](#sustain-loops-samplessustain_loops)
- [Release slides](#release-slides)
- [Sliding loops](#sliding-loops-loop_decay-slide)
- [Settings](#settings)

## Sample length versus notes

A sample that does not loop must last as long as the longest note that plays it, measured in the MOD's
own time at the sample's playback rate.  `sustain_duration: auto` computes that per instrument
(`SustainPlanner`, `core/convert/sustain_plan.py`: 10 s cap, then each generator's cap to
`samples.max_sample_kb`), and `sustain_short` warns where a note still outlasts its sample.  The rules
are in [../synthesis/fm.md](../synthesis/fm.md).  A looped sample holds any note and warns nothing.

Example: a lead holding one note for 3 s at a 16.6 kHz sample rate (`root` C3) needs ~50 KB unlooped;
looped, a few KB.

## Tails no note reaches (`samples.trim_tails`)

The lengths above are estimates from the song's ticks: a ring rounded to where the row grid can put its
ends, a release padding, a merged [mix](../glossary.md#mix) cut at its notes' heard points, a
[bank](../glossary.md#bank) sound followed by a tick of silence.  Once the MOD is laid out, `heard_bytes` (`core/mod/reach.py`) plays the written file as
ProTracker does, tick by tick, and finds how far into each one-shot sample any note is heard (its volume
above 0: `C00`, `ECx` or a slide to 0 ends that); `ModFile.trim_tails` cuts each there, in whole words.

- A looping song is played once more from its loop target, so a note ringing across the loop is
  followed through it; a note still heard at the end (a stopping song's last notes, one that rang a
  whole second pass) keeps its whole sample.
- The cut errs long: every step 3 % fast (finetune tables, an NTSC Paula clock, a player's DMA start), a
  vibrato row at its swing's highest pitch, the note an `EDx` note replaces at the faster of the two
  periods.
- Lossless: Green Hill lofi's merged build loses 5.7 KB, every note playing the same bytes.

Left whole:

- looped samples;
- samples read from a file (`sample_list`: the DAC drums): the same drum in several MODs stays the same
  bytes, so a build that shares samples across MODs can store it once and cut it to its longest use;
- every sample of a MOD with an effect the replay does not model: anything but
  `3xx 4xy 8xx 9xx Axy Bxx Cxx Dxx Fxx E0x E1x E2x E8x EAx EBx ECx EDx`.  The converter writes `1xx` /
  `2xx` (a too-slow modulation's slides) and `6xy` (a sliding loop on a vibrato row), so those songs
  keep their tails; `--verbose` reports it (`tails_kept`).

## Sustain loops (`samples.sustain_loops`)

Many voices settle to a steady tone after their attack: an organ, a held lead.  Rendering such a voice
for its longest note wastes bytes on seconds of the same sound.  A
[**sustain loop**](../glossary.md#sustain-loop) cuts the sample where its envelope settles plus one
loop, so its length no longer depends on the notes.  `samples.sustain_loops` chooses the builds: `off`,
`merged` (code default: the `--merged` build only) or `all` (shipped).  `find_sustain_loop`
(`core/audio/loops.py`) works in five steps:

```
level (dB)
  0 ┤ ╱╲
    │╱  ╲___
    │       ╲______~~~~~~~~~~~~~~~~~~~~~~~~      ← within loop_drift_db of the reference span's end
    │              ^ flat point   [loop]  ✂ cut      reference span: last 1 s of the longest note
    └──────────────────────────────────────────► time
```

1. **Reference:** the RMS envelope (windows of two fundamental periods, dB below the peak) over the last
   second (`SPAN_SECS`) of the instrument's longest note, never the attack.
2. [**Flat point**](../glossary.md#flat-point): the first window after which every window stays within `loop_drift_db` (default 1 dB)
   of the reference span's end level, widened by the span's swing around its trend.  A band, not a
   level, so a beating chorus pair is flat once the beat is steady; detrended, so a decaying voice is
   not flat across its decay.  The span's own windows are checked too.
3. **Timbre:** a loop starts only where the harmonic profile (harmonics 1–8, `PROFILE_PER_DB` 0.25 per dB
   of drift) holds until the longest ring ends: Sonic 1's Spring Yard voice $05 is level-flat from 20 ms
   while its second harmonic swings 30 dB.  Merged builds skip this unless the song sets
   `merge_loop_timbre: true` (it grows the samples).
4. **Length:** every even length (a MOD loop is in words) from 30 ms to `MAX_LOOP_SECS` (1.2 s: a pair
   beating at 1 Hz needs a whole beat), from up to six starts a period apart, scored by the discontinuity
   at the join (two periods after the start against two after the end) plus `LENGTH_PENALTY` per second
   of loop and, with the timbre check, of the bytes between the flat point and the start.  Never a whole
   number of cycles: every Sonic 1 voice detunes its operators (DT1), so the best length is where their
   phases come closest to recurring.
5. **Close:** `apply_loop` crossfades the loop's last 15 ms into the samples before its start and cuts the
   sample at its end.

No loop where the reference has decayed below −50 dB (percussive), where the loop would end later than
80 % of the plain render (`MAX_END_FRACTION`: still settling), or where the raw discontinuity exceeds
`MAX_ERROR` (0.75, ≈ −2.5 dB: uncorrelated).  The generators search a 4 s probe render (`PROBE_SECS`) and
fall back to the plain one.  PSG tones loop the same way; noise never loops.  A voice under the hardware
LFO loops over whole LFO cycles ([detune.md § Hardware LFO](detune.md#hardware-lfo)).

### Tuning

`loop_drift_db` is the size / fidelity knob (settings, song or entry level): at 1 dB a slowly decaying
voice loops only near the end of its longest note and keeps its decay; at 3–12 dB it loops earlier,
smaller, and its long notes end louder than the hardware's.

Per entry (or merge group):

| Key | What it does |
|-----|--------------|
| `loop_drift_db` | as above, for this entry |
| `loop_min_ms` | the shortest loop (an early short loop buzzes) |
| `loop_start_ms` | the earliest start: a detuned pair's swing spans the band from its first window, so its loop may start in the attack (without it, Sonic 1's 1-Up lead pair loops at 21 ms and replays its onset on every beat) |
| `loop_decay: slide` | [§ Sliding loops](#sliding-loops-loop_decay-slide) |

```yaml
# configs/sonic_1/02_green_hill_zone.yaml: the lofi variant's song-wide drift, held back on one entry
variants:
  lofi:
    loop_drift_db: 20             # loops may freeze 20 dB above the settled level: shorter samples
voice_map:
  5:
    - low:  C4
      high: B5
      mod_instrument: 9
      root: C2
      variants:
        lofi:
          loop_drift_db: 1        # a loose drift loops this voice 27 ms long, 13 ms in: a buzz on every note
```

## Release slides

A looped sample rings until something stops it, so with loops on every FM note ends with a [**release
slide**](../glossary.md#release-slide), a volume slide at the voice's release rate, instead of `C00` (`Fades.release`,
`core/convert/channel_writer/fades.py`):

- `release_rate_db_s` fits the dB/s slope of the render's tail after key-off;
- one `A0y` per row from the rest's (or the fill's) row takes the volume to where that slope is at the
  row's end.  The chip's release is linear in dB, so each row's target is the last one's times a fixed
  ratio;
- rows whose share rounds to nothing are skipped, a taken slot is skipped, the slide stops at the next
  note-on's row, and after 64 rows a `C00` ends what is left (release rate 0 rings forever).

Example (illustrative): volume 32, release 60 dB/s, a row of 60 ms (speed 3 at 125 BPM): 3.6 dB a row, ×0.66 → 21, 14,
9, 6 … each row an `A0y` of the needed units over the row's `speed − 1` ticks.

Exceptions:

- A release that falls 30 dB within a row (RR $0F: every Sonic 1 Title Screen voice) stays a cut.
- PSG notes keep their cuts (attenuation 15 is instant).
- On a column another channel's notes borrow, the end is a plain `C00`.
- `Cells.clear_stale_cut` removes a rest's `C00` from a cell a later note-on rounds onto (a note-on with
  `C00` is silent).

### A key-off on the attack row

A key-off on its note's own attack row (a note shorter than a row: Time Trax's 1-frame notes, Streets of
Rage 2's 1-tick hits) cannot take a `C00` there: the note would sound nothing.

- Where the release is a cut: `ECx` at the key-off's MOD tick, at least 1 (`_cut_in_attack_row`).
- Otherwise, or with the slot taken: the key-off waits a row, and is dropped where the channel's next
  note lands on that row (its slide would fade the new note).

Evidence: Streets of Rage 2's Jungle Base FM3 `$1B` hits read 7.4 dB quiet against the rip when muted by
an attack-row `C00`, and 15 dB loud when heard a whole row each.

## Sliding loops (`loop_decay: slide`)

A [**sliding loop**](../glossary.md#sliding-loop) loops a voice that keeps falling and writes the fall as volume slides.

A voice whose level falls for as long as it holds (an FM bass on a non-zero D2R) never settles: the
default `freeze` loops it near the end of its longest note, or at a large drift holds it too loud.

With `slide` (entry or merge group) `find_sustain_loop` follows the reference span's trend line:

- flat is every window back from the span within `loop_drift_db` of that line extended, the slope
  refitted over them, the timbre check always on;
- a fall under 0.1 dB/s (`MIN_DECAY_DB_S`) is a plain loop;
- the render is flattened from the flat point (`flatten`), the loop found and closed in it, and the fall
  (`SustainLoop.decay_db`, `flat_at`) goes to the converter.

`Fades.decay` writes the fall into each note: on every row it rings through after the attack row, a
slide toward `volume × 10^(−fall × (t − t0) / 20)`, with *t0* and the fall at the rate the note's period
plays the sample (a note above the root falls faster, as the unlooped sample did).

- `A0y` where the row's share is at least one `A01` (`speed − 1` units), else `EBx` (Sonic 1's Game Over
  bass falls a unit a row at speed 9: `A01` every eighth row would be a staircase); `6xy` on a row
  carrying the note's `4xy`.
- Targets are absolute, so a taken row is made up on the next; a `Cxx` row resets the tracked volume.
- The slides stop at the ring's end (its rest's release starts from the fallen volume), the next note-on
  or the fill.

Not handled: a sliding instrument as a merged mix source (the mix has no slides), a `3FF` note (its
curve restarts), PSG.

## Settings

The settings this page names (`samples.sustain_loops`, `loop_drift_db`, `trim_tails`, `max_sample_kb`,
`pt_zero_bytes`), with code defaults and shipped values: [../guide/settings.md](../guide/settings.md#samples).
