# Spring Yard Zone — accuracy audit against the VGZ (2026-09-18)

`configs/04_spring_yard_zone.yaml` against `reference/vgz/04 - Spring Yard Zone.vgz`, method as in
`02_ghz_audit.md` and `03_mz_audit.md`.

## Result: clean; no config change

| Check | Result |
|-------|--------|
| Pitch (symbolic) | **374 / 380**.  The six "wrong" notes are PSG1's opening riff (0.6–2.2 s), written one to three semitones *below* the PSG table — see below.  Everything else, including FM4/FM5's 1289 grace-note arpeggio steps, is at the hardware's pitch |
| Key-ons | 0 unmatched on every channel except one key-on at **0.000 s** on FM3 and FM4 that the source has no note for (FM3 starts with a 96-tick rest): the driver's song-start register state, not a note.  It is also the report's one `SILENT in MOD` line |
| Timing | median onset error 0–1 ms on every channel; FM4/FM5/PSG1's odd-tick grace notes carry `EDx` (worst +34 ms is the pushed slide target) |
| Levels | DAC +0.1, FM1 +0.7, FM3 −0.2, FM4 −0.2, FM5 −0.2, NOISE +0.2 dB relative to FM2; every FM instrument's per-note error within ±0.4 dB, FM4/FM5 share instrument 5/6 at +0.0 |
| PSG1 level | see open items: the channel RMS reads −3.9 dB while its notes read +0.5 (62 notes, `fTone_06_hi`) and +4.9 (8 notes, `_lo`).  The +4.9 is not real: 6 of those 8 notes are the riff below the table, two of which the hardware plays *inaudibly* (divider 0) — the per-instrument suggestion (9 → 5) was **not** applied |
| Vibrato | FM1 F5 (9.5 s): hardware 5.99 Hz ±25 c, MOD 6.24 Hz ±19 c.  FM4/FM5 A5: hardware ±3 c, MOD none by design (below one depth step).  FM1 C5/F5 at 25.7 / 30.6 / 35.3 s beat on hardware (`b`, 4.9–5.2 Hz) and not, or at 5.78 Hz, in the MOD — detuned carriers resampled, todo item 7 |
| Noise | rate-3 hi-hat, derived divider 1; +0.1 dB, 263/263 hits |
| PSG2 | the source has a PSG2 header but no notes; the config is right to omit it |

## PSG notes below the table

PSG1 opens with `As3 A3, C4 B3, Cs4 C4, D4 Cs4` grace-note pairs under transposition −48: table
indices −2/−3, 0/−1, 1/0, 2/1.  The driver masks the index to 7 bits and reads past the 70-entry
table into the `CoordFlag` code that follows it.  The recording shows what those bytes are:

| Index | Written | Divider read | Sounds |
|------:|---------|-------------:|--------|
| 125 | A3 (−3) | 0 | nothing (inaudible) |
| 126 | A#3 (−2) | 922 | B2 |
| 127 | B3 (−1) | 540 | G#3 |

Credits PSG1 hits the same three indices and its recording shows the same dividers, so
`sfx/tables.py` now carries them at the end of `PSG_FREQUENCIES_EXTENDED` (the chromatic
extrapolation stays for 70–124, where no recording reaches).  With `range_space: chip` a song
reproduces the hardware there; Spring Yard's config is in source space and plays the *written*
pitches, which is musically what was meant — six notes of a two-second intro, left as they are.

## Open

- **PSG1 channel RMS −3.9 dB** against per-note errors of +0.5 dB.  Same shape as Marble Zone's DAC
  (+2.1 channel, per hit ≤ 0): the whole-song figure and the per-note figure disagree and the cause
  is not established.  Nothing was changed on that evidence.
- FM1 detuned-carrier beating (item 7).
