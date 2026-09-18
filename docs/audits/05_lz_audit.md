# Labyrinth Zone — accuracy audit against the VGZ (2026-09-18)

`configs/05_lab_zone.yaml` against `reference/vgz/05 - Labyrinth Zone.vgz`, method as in
`02_ghz_audit.md`.  Two config fixes, one audit-tool fix.

## Result

| Check | Before | After |
|-------|--------|-------|
| Pitch (symbolic) | FM 253 / 253; **PSG1 and PSG2 not judged at all** (every segment "shorter than 60 ms") | **405 / 405** on every channel (FM3/FM4's 1-tick arpeggios audited at `--min-ms 25`: 343 / 343) |
| Key-ons | 0 unmatched everywhere; PSG1/PSG2 15 MOD-only each (repeated notes at the same pitch, `nA6, $03, nA6`, which the key-on detector cannot see on a PSG) | same |
| Timing | median +1…+2 ms on every channel, no drift (250 BPM exact after item 14's speed 4) | same |
| Levels | DAC +0.8, FM1 −0.2, FM3/FM4 +0.2, FM5 −0.1, PSG1 −0.2, PSG2 −0.6, NOISE −0.4 dB relative to FM2; every instrument's per-note error within ±0.4 dB, FM3/FM4 share their level | PSG1 +0.1, PSG2 −0.7; unchanged elsewhere |
| Noise | rate-3 hi-hat, derived divider 1, 137 / 137 hits | — |

## Fix 1: the PSG instrument was an octave high

`fTone_09`'s `synth_root: D6` → **D5**.  The survey's audit never saw it: `chip_timeline` started a
new segment on every PSG register write, and `fTone_09` writes the volume every frame, so PSG1's
120 ms notes became 17 ms slivers below the 60 ms cut and were reported as "shorter than 60 ms"
(1084 of them) instead of as an octave error.  Segments now end only when the pitch or the
audibility changes (`tools/vgm_pitch_audit.py`); the other audited songs read the same as before
(GHZ 928 / 928, Scrap Brain 10 wrong, Credits 7, Stage Clear 12 — the totals rose because notes
that were slivers are now counted).

## Fix 2: PSG root anchor against `smpsAlterPitch`

With the octave right, 48 PSG notes were still −300 / −500 / −800 c (16 each): the psg_voice_map
entry had `low: D6` / `root: D2`, and a root anchor ignores `smpsChangeTransposition` — the gotcha
CLAUDE.md states for FM applies to PSG entries with `low` too.  PSG1 and PSG2 run `Loop06` with
`smpsAlterPitch $05` twice and `$F6` after it.  The entry is now rootless (no `low`/`high`) and the
channels carry `transpose: -12`, so the notes follow `total_transpose`; the sample still plays D5
at MOD D2.

## Vibrato

FM3's `smpsModSet $01,$01,$01,$04` (delta 1, steps 4: ±2 FNUM ≈ ±3.5 c) is never switched off, so
every later FM3/FM4 note modulates on hardware — the estimator catches it on the G5 at 35.5 s
(5.99 Hz ±3.8 c) and not on the E5s at 21.1 / 28.8 s.  The MOD's smallest depth is one step, ±9 c
on E5 (5.85 Hz), and nothing on G5.  Quantisation on a ±3.5 c effect, not an error.

## Open

- Nothing config-side.  FM4 G5 at 35.5 s beats at 4.5 Hz in the MOD (`b`) — detuned copy of FM3,
  item 7 / 8 territory.
