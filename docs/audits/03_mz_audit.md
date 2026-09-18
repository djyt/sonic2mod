# Marble Zone — accuracy audit against the VGZ (2026-09-18)

`configs/03_marble_zone.yaml` against `reference/vgz/03 - Marble Zone.vgz`, with the tools as they
stood after todo items 1–6, 9–12 and 14 (frame-timed fills, the vibrato formula, `EDx`, baked levels,
derived rate-3 noise, tempo changes) and the Credits work.  Method as in `02_ghz_audit.md`:

```
python tools/vgm_pitch_audit.py configs/03_marble_zone.yaml "reference/vgz/03 - Marble Zone.vgz"
python tools/vgm_compare.py    configs/03_marble_zone.yaml "reference/vgz/03 - Marble Zone.vgz"
```

## Result: clean, one volume set by hand

| Check | Result |
|-------|--------|
| Pitch (symbolic) | **731 / 731** notes at the hardware's pitch; every note on the row grid (2 ticks per row, divider 2 × `ticks_per_row: 1`), no `EDx` needed |
| Key-ons | 0 unmatched on every FM, PSG and noise channel; 1 MOD-only re-trigger on FM1/FM2/FM3/PSG1/PSG2 (a tie the hardware holds), 3 on NOISE |
| Timing | median onset error −4…+5 ms per channel, no drift (200 BPM is exact) |
| Levels, FM / PSG | FM1 −0.2, FM3 −0.2, FM4 −0.4, FM5 −0.4, PSG1 −0.1, PSG2 −0.7, NOISE −0.4 dB relative to FM2; every instrument's per-note error within ±0.6 dB, channels sharing an instrument agree (inst 5: FM4 −0.2 / FM5 −0.1) |
| Snare | +2.8 dB on its 3 hits — too few for `--write-volumes` (needs 4); **set 64 → 46 by hand**, reads −0.1 dB after |
| Kick | −0.3 dB per hit at 64; unchanged |
| Vibrato | the song has no `smpsModSet`.  FM4's closing G4 (37.8 s) beats at 2.67 Hz ±14 c on hardware (detuned carriers, `b`) and not at all in the MOD — the shared voice-$02 sample is resampled far from that note → todo item 7 |
| Noise | pitched rate-3 noise: the derived divider is 34 (nA3 + 11) and the MOD follows the melody by playback speed — the first hits read ratio 1.49 = divider 23, as the recording has.  Only **2 of 160** hits are the white `nMaxPSG` hi-hat (divider 0), which a single noise sample cannot also be; those two sound pitched.  Whole-song level −0.4 dB |
| DAC rate | the snare's "low peak" read 227 Hz on hardware against 314 Hz in the MOD (GHZ's read 308), which looked like a rate error.  Counting PCM writes per second in the VGM settles it: the snare plays at **22.9 kHz in both songs**, the kick at 7.9 kHz — the spectral-peak column is picking different bumps in a noisy sample.  Measure DAC rate this way |

## Open

- **DAC channel reads +2.1 dB** relative to FM2 as a whole-song RMS, while every per-hit measure
  says the kick is 0.3–1.6 dB *quiet* and rings 170 ms against the hardware's 210 ms, and the snare
  is now −0.1 dB.  The two measures disagree and the cause is not found (the recording's DAC channel
  also has a 3.4 kHz PCM segment at 0.35 s that the parser's kick/snare list does not account for).
  The kick stays at 64: turning it down would make the per-hit figures worse.
- FM4/FM5 detuned-carrier beating (item 7).
- The two white hi-hat hits inside the pitched-noise part (inherent with one noise sample).
