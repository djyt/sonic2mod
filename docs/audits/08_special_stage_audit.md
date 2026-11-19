# Special Stage — accuracy audit against the VGZ (2026-11-19)

`configs/sonic_1/08_special_stage.yaml` against `reference/vgz/sonic_1/08 - Special Stage.vgz`, method as in
`02_ghz_audit.md`.  First audit 2026-09-18; this one re-runs every tool on the current converter.
The config's grid is `row_grid: bar` since this audit; the figures are for that build unless marked.

| Check | Result |
|-------|--------|
| Pitch (symbolic) | **381 / 381** chip segments of 60 ms or more; `--slides`: FM3–FM5 165 / 165 each (their `smpsModSet` steps) |
| Lift / frames | FM plays as the song; `vgm_frames` 0 misses (pitch, level, voice, pan).  PSG lengths: the lift's unfinished PSG reading, every song alike |
| Key-ons | 0 unmatched on every channel; PSG2 27 MOD-only (repeated notes at one pitch, invisible to the PSG key-on detector) |
| Timing | median 0…±1 ms, worst ±10 ms; no drift (175 BPM, the driver's tempo exactly).  The finest grid (`ticks_per_row: 2`, speed 6, 196.875 → 197 BPM): worst ±17 ms |
| Levels | every instrument within ±0.9 dB per note, none to change; FM3/FM4/FM5 share instrument 4 and agree.  Whole-song RMS against FM2: FM1 +0.9, FM3–FM5 +0.9…+1.0, FM6 +0.4, PSG1 −0.3, PSG2 +0.5 dB |
| Vibrato | FM3–FM5 `smpsModSet`: hardware 4.25 Hz ±18…32 c, MOD 4.07–4.11 Hz ±20…29 c (`4xy` granularity); FM4's last note ±26 c against ±18 |
| Beating | FM6's detuned carriers (voice $01).  Its sample is synthesised at E5, the commonest note: E5 4.44 Hz (hardware 4.46), G5 5.29 (4.86, +9 %), C5 3.54 (4.46, −21 %).  The long notes are C5 |
| MOD | `mod_lint` 0 issues; every note fits its sample; 6 samples, 95 KB; 3 patterns of 48 rows |
| Channels | DAC and PSG3 are in the header but have no notes in the source and nothing in the recording; 8 channels |

## Grid: `row_grid: bar`

A 16th (6 duration units) a row, speed 16, 175 BPM, 3 patterns of 48 rows, one per section.  Every
note-on on a row; `$03` notes end on an `EC8`.  Against the finest grid (`ticks_per_row: 2`, speed 6:
`$03` notes 1.5 rows, 7 patterns): pitch, slides and levels the same, timing closer (speed 16 puts
`EDx` nearer the frame).

## Fixed in this audit

- `vgm_compare`'s vibrato table read FM6's beat as missing in the MOD: pitch was measured on the render
  moved onto the rows' clock, whose repeated frame each tick reads as a 79 Hz wobble.  Pitch now reads
  the render as written (and no longer 2 c flat).
- `vgm_pitch_audit --slides`: one false miss per FM3–FM5 at 8.90 s, a frame straddling the MOD's
  change from C5 to D5.
- The converter wrote a `C00` a row after a note already cut on its own row (the `bar` build: 31 on PSG1).
- `sample_list` comments: stale levels removed.

## Open

- FM6 beat rate on C5 and G5.  Split voice $01 by note (C, E, G samples, ~41 KB each); deferred to the
  4-channel build.
- FM1 (voice $00, detuned carriers too): beat 1.3–2.0 Hz in the MOD against 1.5–1.8 Hz; same mechanism, minor.
