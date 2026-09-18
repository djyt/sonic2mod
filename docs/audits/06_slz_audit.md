# Star Light Zone — accuracy audit against the VGZ (2026-09-18)

`configs/06_star_light_zone.yaml` against `reference/vgz/06 - Star Light Zone.vgz`, method as in
`02_ghz_audit.md`.  The config moved to `range_space: chip`; one volume set from the measurement.

## Result

| Check | Before | After |
|-------|--------|-------|
| Pitch (symbolic) | 795 / 819 — FM2's bass 24 notes 100–300 c high | **819 / 819** |
| Key-ons | 0 unmatched on every channel (FM5 3 MOD-only re-triggers) | same |
| Timing | median +2…+3 ms, no drift (250 BPM exact since item 14) | same |
| Levels | DAC −0.4, FM1 −1.2, FM3/FM4 +0.6, FM5 +0.3, PSG1 +0.2, PSG2 +0.5, NOISE −0.1 dB relative to FM2 | kick 45 → 56 (read −1.9 dB); FM1 unchanged: its two instruments are at 64 and still read −1.9 / −2.1 dB, the ceiling |
| Vibrato | the song has no `smpsModSet`; no beat rows either | — |
| Noise | rate-3 hi-hat, derived divider 1, 267 / 267 hits, +0.3 dB | — |

## FM2: `root` against `smpsAlterPitch`, solved with `range_space: chip`

FM2 walks its bass line down by transposition — `smpsAlterPitch $FF` three times (D → C# → C → B)
— and voice 1's entry anchored the *source byte* to D2, so all three lower steps played D2.  This
is the FM twin of Labyrinth Zone's PSG case, and unlike PSG a rootless FM entry is not
synthesised, so the fix is the song-level `range_space: chip`: every `low`/`high` is now the pitch
the chip plays (source byte + the channel's `pitch_offset`: FM1/FM2 −24, FM3/FM4 −36, FM5 −12,
PSG −60 through the driver table), `root` and `synth_root` unchanged.  The old source bytes are
kept in comments.

One entry needed more than the offset: voice 5's single note is written `nDs4` but played under
`smpsAlterPitch $33` (+51) — chip F#7, which the entry already synthesised at (`synth_root: Fs7`).
Its range is `Fs7`–`Fs7` now; in source space that note had sat behind a coincidence.

## Open

- **DAC channel RMS +1.5 dB** after the kick change, while the kick reads within ±1 dB per hit —
  the same channel-vs-note disagreement as Marble Zone's DAC and Spring Yard's PSG1.  Volumes
  follow the per-note figure until that is understood.
- FM1 −1.2 dB with both of its instruments at 64: the only way up is everything else down, which
  `--write-volumes` does not do for a 2 dB shortfall.
