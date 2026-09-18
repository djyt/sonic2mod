# Scrap Brain Zone — accuracy audit against the VGZ (2026-09-18)

`configs/07_scrap_brain_zone.yaml` against `reference/vgz/07 - Scrap Brain Zone.vgz`, method as in
`02_ghz_audit.md`.  One config fix; one converter rule corrected back in the config's favour.

## Result

| Check | Before | After |
|-------|--------|-------|
| Pitch (symbolic, 60 ms) | 1203 / 1213 — FM4's 10 are `smpsAlterNote` detune scoops (+44…+54 c), item 8 | same 10; every other note right |
| Pitch, PSG2 at 25 ms | **0 / 512**: instrument 15 (`fTone_05`) an octave high on every note | **516 / 516** |
| Key-ons | 0 unmatched on every FM, PSG and noise channel (FM2 554 / 554, NOISE 416 / 416) | same |
| Timing | median −3…+4 ms, no drift (180 BPM exact) | same |
| Levels | DAC +1.2, FM1 −0.2, FM3 −0.2, FM4 +0.3, FM5 −0.3, PSG1 −0.1, PSG2 +0.5, NOISE +0.0 dB relative to FM2; every instrument within ±1.4 dB per note (kick −1.6 at the 64 ceiling) | PSG2 +0.9 channel / +0.2 per note after the octave change |
| Vibrato | FM4 D4 (15 s note): hardware 5.98 Hz ±24.5 c, MOD 5.99 Hz ±15.8 c (one depth step short); FM1 A5/E5/G5 beat at ~5.0 Hz on hardware and 5.2–5.3 Hz in the MOD (`b`, detuned carriers — close) | — |
| Noise | rate-3 hi-hat on three noise instruments (one per envelope: `$E7` default, `fTone_04`, `fTone_08`), derived divider 1 each; +0.0 dB channel, 416 / 416 hits | — |

## Fix: PSG2 an octave high

`fTone_05`'s `synth_root: G6` → **G5**.  Every PSG2 note is shorter than 60 ms (512 of them), so the
default audit never judged the instrument; `--min-ms 25` showed all 512 an octave high.  Second
song after Labyrinth Zone where the short-note cut hid an octave error — a PSG channel that reports
hundreds of "shorter than 60 ms" needs a 25 ms pass.

## Converter rule: noise-mode envelope variants

This config had already solved the PSG3 question the right way: after `smpsPSGform $E7` the track
is a noise channel for good, and its later `smpsPSGvoice fTone_04` / `fTone_08` only change the
hi-hat's envelope — so `psg_voice_map` lists those labels as **noise** entries with their own
instruments (16, 18), each with the right baked level.  The Credits work had made the converter
ignore every `smpsPSGvoice` in noise mode, which took those variants away (406 cells, wrongly
accepted as a fix at the time).  The rule is now: in noise mode a `smpsPSGvoice` is honoured when
its entry is a noise type and ignored when it is a tone (Credits' labels belong to PSG1/PSG2).
Scrap Brain's output is byte-identical to its baseline from before the Credits commit.

## Notes on the tools

- FM1's `smpsModSet $0D,$01,$08,$05` swings ±50 c, so a 25 ms audit sees vibrato steps beyond
  50 c and calls 231 of them wrong notes at ±100 c.  Judge FM at the 60 ms default; the 25 ms pass
  is for PSG channels whose notes really are that short.
- 657 `<-- PITCH` flags in the per-note audio table are the same vibrato; the symbolic verdict is
  the one that counts.

## Open

- FM4 `smpsAlterNote` scoops (10 notes) and FM1 carrier beating — items 8 and 7.
- FM5 B3 at 0.865 s: hardware 4.46 Hz ±5.9 c, MOD 5.99 Hz ±13.1 c (its `4xy` is FM4's).  Not examined.
