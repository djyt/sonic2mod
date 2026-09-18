# Special Stage — accuracy audit against the VGZ (2026-09-18)

`configs/08_special_stage.yaml` against `reference/vgz/08 - Special Stage.vgz`, method as in
`02_ghz_audit.md`.  No change needed.

| Check | Result |
|-------|--------|
| Pitch (symbolic) | **507 / 507**, at 60 ms and at 25 ms (PSG1 88, PSG2 21); FM3–FM5's 175 sub-60 ms segments are their `smpsModSet` steps |
| Key-ons | 0 unmatched on every channel; PSG2 27 MOD-only (repeated notes at one pitch, invisible to the PSG key-on detector) |
| Timing | median 0…−2 ms, worst ±17 ms; no drift since `target_speed: 6` (item 14 — speed 3 ran 0.44 % slow) |
| Levels | FM1 −0.4, FM3 −0.2, FM4 +0.0, FM5 −0.0, FM6 −0.5, PSG1 −0.3, PSG2 −0.2 dB relative to FM2; every instrument within ±0.2 dB per note, FM3/FM4/FM5 share instrument 4 at +0.0 / +0.2 / +0.1 |
| Vibrato | FM3–FM5 `smpsModSet`: hardware 4.25 Hz ±20…32 c, MOD 4.06 Hz ±20…30 c (measured in the item 2 work) |
| Beating | FM6's detuned-carrier beat is 4.45 Hz on C5 in both renders — the sample is synthesised at C5 — and 5.6 / 6.7 Hz in the MOD on E5 / G5 where the hardware stays at 4.45 / 4.86: a resampled sample beats faster.  Item 7, the clearest example so far |
| Channels | DAC and PSG3 are in the header but have no notes in the source and nothing in the recording; the 8-channel config is right |

## Open

- FM6 beat rate (item 7).  A second FM6 instrument at E5/G5 would fix it at the cost of two samples.
- FM1 G2 at 31.5 s: 2.4 Hz ±20 c beat in the MOD only — the same mechanism on voice 0.
