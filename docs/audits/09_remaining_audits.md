# Robotnik, Final Zone, Stage Clear, Ending, Invincibility, 1-Up, Chaos Emerald, Drowning, Continue, Game Over — audits against the VGZs (2026-09-18)

The ten configs left after `08_special_stage_audit.md`, done in one pass with the method of
`02_ghz_audit.md`: symbolic pitch audit at 60 ms and 25 ms, rendered comparison, `--write-volumes`
where the per-instrument table asked for it, re-render, re-measure.

## Pitch

| Song | Before | After | What was wrong |
|------|--------|-------|----------------|
| Robotnik | 249 / 249 | — | clean (the 25 ms pass flags 50 vibrato steps on FM1/FM4, not notes) |
| Final Zone | 271 / 271 | — | clean |
| Stage Clear | 62 / 74 | **74 / 74** | PSG1 (`$D0`) and PSG2 (`$DC`) share `fTone_05` entries tuned for PSG2; PSG1's notes came out an octave high, its high entry 5 semitones — the shared-entry case, solved by `range_space: chip` |
| Ending | 190 / 196 | **194 / 196** | PSG2 (`$DC`) plays E6–B6 through PSG1's (`$D0`) `fTone_05` entry, which never covered them; in chip space they need their **own instrument** (12) — at the shared sample's tuning they would sit above MOD B3.  The two left are B6 at +49 c: PSG divider granularity at that pitch (a step is 31 c there) |
| Invincibility | 140 / 204 | **204 / 204** | voice 0 shared by FM1/FM5 (`pitch_offset` −12) and FM3/FM4 (−24) through one source-byte entry: the −24 channels an octave high.  `range_space: chip` |
| 1-Up | 48 / 48 | — | clean |
| Chaos Emerald | 65 / 65 | — | clean |
| Drowning | 371 / 371 | — | clean at 60 ms; at 25 ms FM3's 50 ms notes read ±100 c because the MOD is one to two frames behind at each tempo step (item 14's inherent lag), not because of pitch |
| Continue | 70 / 101 | **101 / 101** | FM2 changes key with `smpsAlterPitch` (+1, −12, +12, +1 …) under source-byte roots; `nFs5` / `nE4` were never covered at all.  `range_space: chip`, with the loop-extended events (the transposition accumulates on every replay) |
| Game Over | 66 / 66 | — | clean (2 FM1 notes at 25 ms are vibrato) |

Stage Clear, Ending, Invincibility and Continue were converted with `tools/config_to_chip_space.py`,
written for this pass (see `docs/pipeline.md` § `range_space: chip`).  Three things it had to get
right, each found on one of these songs: keep `synth_root` and `root` moving together (the sample
is rendered from the entry's own pair — Ending read 19 semitones off with `root` moved alone);
collect every note a source byte plays under every transposition, not the last one (Continue's
E4 → E2); and refuse to clamp a range that shares an instrument, since that retunes the sample the
other entries rely on (Ending's PSG2).

## Levels

`--write-volumes` changed: Final Zone's six FM instruments down 1.2–3.0 dB (its DAC is the anchor:
everything else had to come down to meet the kick at 64); Chaos Emerald's two PSG tones (+6.3 /
+2.8 dB); Continue's `fm_v01` (+1.7); Stage Clear's and Ending's new PSG instruments.  After the
pass every instrument with four or more notes is within ±1.3 dB except at the 64 ceiling (Stage
Clear `fm_v00_hi` −2.7 on 4 notes, Game Over kick −2.0 on 5, Drowning `fm_v02` at 64).

Whole-channel RMS still disagrees with the per-note figures on several channels — Invincibility
FM5 +3.7, Chaos Emerald FM4/FM5/FM6 +2.7…+3.8, Stage Clear PSG2 −2.8, Drowning FM3 −2.5 — while
their notes measure within a dB.  Chaos Emerald suggests the cause: it is long sustained chords,
and a MOD sample is a looped sustain at constant level where the YM2612's envelope keeps decaying
through the note, so the whole-note energy differs while the attack level (what the per-note table
measures over its first 0.6 s) matches.  Volumes follow the per-note figure; the envelope tail is
a synthesis question (with items 7 and 8), not a level one.

## Everything else

- Key-ons: 0 unmatched on every FM/PSG/noise channel of all ten (Drowning's tempo steps handled
  by the matcher's re-sync); the MOD-only counts are ties and same-pitch repeats.
- Timing: no drift except Drowning's known +59 ms over its tempo steps.
- Vibrato: Ending FM4 A1 (15.7 s) — hardware 5.98 Hz ±18 c, MOD none: `smpsModSet $00,$01,$06,$04`
  on A1, where one depth step is 12 FNUM on a 1083 word ≈ ±19 c but the MOD period for that low
  note makes `y` 0.79 → 1 … the estimator found nothing on the MOD render; not chased.  Game Over
  FM1 B3: 4.31 Hz ±20 c vs MOD 5.16 Hz ±9 c — the note is shorter than the wait plus one cycle,
  so the hardware figure is a partial cycle; depth is one step short.
- Robotnik PSG1 −1.8 dB channel RMS with its notes right: the same disagreement as above.

## Open after this pass

- Item 13's pitch list is closed: every song's remaining "wrong" notes are detune scoops (item 8),
  PSG divider granularity, or notes written below the PSG table.
- The channel-RMS / per-note disagreement, now with a hypothesis (envelope decay through long
  notes) worth a direct test: compare the level 1 s into a held chord in both renders.
