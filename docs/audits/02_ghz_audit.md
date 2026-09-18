# Green Hill Zone conversion audit (2026-09-18)

Accuracy audit of `configs/02_green_hill_zone.yaml` → `output/02_green_hill_zone.mod` against
`reference/vgz/02 - Green Hill Zone.vgz`.  Same method as `docs/audits/01_title_screen_audit.md`, plus a
symbolic pitch check that turned out to be necessary for this song.  Reproduce with:

```bash
python convert.py configs/02_green_hill_zone.yaml
python tools/vgm_pitch_audit.py configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz"
python tools/vgm_compare.py    configs/02_green_hill_zone.yaml "reference/vgz/02 - Green Hill Zone.vgz"
```

---

## Verdict

| Aspect | Result |
|--------|--------|
| Notes | **867 of 867** sustained chip segments (≥ 60 ms, all 5 FM + 2 PSG tone channels) sound at the chip's pitch.  Before this audit 2 were wrong — both caused by a parser bug, not the config (below). |
| Timing | MOD and VGZ align at 0 ms; one pass is 52.8 s vs 53.2 s.  Note fills match hardware to the frame after todo item 1 (FM2 `$04` = 4 frames ×158 on hardware, 67 ms ×156 in the MOD). |
| Grace notes | **Lost.**  FM3/FM4/FM5 use `nC6, $01, smpsNoAttack, nB5, $0F` (1-tick grace + legato slide) 17 times each.  At `ticks_per_row: 2` the grace and the main note land on the same or adjacent row: on FM3 the grace collides with a rest's `C00` and is silent, on FM4/FM5 it is overwritten.  → todo item 3, and see "Grace notes" below. |
| Levels (before) | NOISE +6.9 dB relative to FM2, FM5 roughly +3 dB (+5.5 dB on the old mono-mix measure; not re-rendered on the corrected one), everything else within 1 dB (FM4 +0.9).  L/R power — see "Measurement correction" below. |
| Levels (after) | NOISE −0.3 dB, FM5 +2.2 dB, FM4 +0.9, FM1 −0.3, FM3 +0.1, PSG1 −0.7, PSG2 −0.6, DAC −1.0. |
| Why FM5 (and FM4) stay hot | Two separate causes.  **Pan law:** on hardware FM4 is panned hard left and FM5 hard right; a centred YM2612 channel drives both speakers (+3 dB power), every Amiga channel drives one.  FM3 (centre), FM4 and FM5 share instrument 14 at the *same* TL `$12`, and FM4/FM5 still read +1.4 (with a `C1B`) / +2.9 dB against FM3's +0.2.  **Volume law:** with `fm_volume_scaling: false` one `smpsAlterVol` step becomes one *linear* MOD volume unit (≈ 0.3 dB) instead of 0.75 dB and the header TL is ignored.  FM5's `$13`→`$19` step is −4.5 dB on the chip and −0.85 dB in the MOD: predicted error +3.6 dB, measured +3.4.  FM1's fade `C1A`→`C1F` drifts from +5.8 to +0.4 dB.  → todo item 4. |
| Noise timbre (before) | `synth_root: A8` → LFSR at 7 kHz: energy bunched below 4 kHz (0–500 Hz −6.8 dB vs −14.4 dB on hardware). |
| Noise timbre (after) | `tone2_n: 1` (the VGZ shows tone-2 divider N=0 on every hit): 4–8 kHz band −4.7 dB vs −4.9 dB on hardware.  8–13 kHz still ~7 dB down (27.9 kHz sample rate), as on the Title Screen. |
| Detuned-carrier beating | FM4/FM5 C6 notes beat at 4.46 Hz on hardware and 6.5 Hz in the MOD; FM3/FM4 low notes show 2.5–3 Hz beating in the MOD only.  Not vibrato — those channels have no `smpsModSet`.  The rate follows sample playback speed (voice $04 is synthesised at C5 and played at C6 and above) → todo item 7. |
| Vibrato | FM1's `smpsModSet $0D,$01,$07,$04` is switched off in the config (`vibrato: 0`, "sounds awful") — expected until the `4xy` formula is fixed (todo item 2).  PSG1's `$0E,$01,$01,$03` is emitted but cannot be measured yet (the analyzer reports every PSG period change as a key-on). |
| DAC | Snare 308 Hz vs 315 Hz (+2 %), kick 52.5 Hz vs 55.2 Hz (+5 %); band profiles match.  Level −1.0 dB with the samples already at 64. |
| Size | 875 KB → 760 KB (two 57 KB samples that carried one note each removed). |

---

## Bug found: coordination flags were applied one note early

`core/smps_parser.py` kept a note byte that has no duration byte of its own "pending" while it
emitted the coordination flags that followed it.  GHZ FM3:

```asm
	smpsCall            Mus81_GHZ_Call02   ; ... nD7, nB6   <- last note has no duration byte
	smpsSetvoice        $08
	smpsAlterPitch      $E8
	smpsAlterVol        $FE
```

came out as `Setvoice $08 / AlterPitch −24 / AlterVol` **then** `nB6` — the last intro note played
on voice $08's instrument, two octaves low.  In the driver (`FMDoNext`/`PSGDoNext`) the byte after
a note is either a duration or it is put back (`subq.w #1,a4`) and the note plays with the saved
duration, so a flag can never come between a note and its duration.  The parser now completes the
pending note before any flag, `smpsCall` and `smpsReturn`.

Effect on the regression songs: GHZ 52 cells (6 note/instrument changes), SBZ 7 cells (1), the
other three unchanged.  It also moved FM1's `smpsNoteFill $0B` onto the right notes — 183 ms cuts
×5, matching the hardware's 11-frame key-offs ×5 (was ×4) — and FM2's `$04`/`$00` alternation.

The config had been worked around it: `voice_map[4]` and `channel_instrument_map.FM5[4]` each had
a one-note `B4` entry (instruments 7 and 9, `ghz_v04_lo.raw`) for a trailing `nB4` that really
belongs to the *previous* voice.  The FM5 copy used `synth_root: B3` where FM4's used `B2`, which
is the second wrong note the audit found.  Both entries are gone.

---

## Config changes made

1. `psg_map[0xE7]`: `synth_root: A8` → `tone2_n: 1`; `psg_noise.raw` volume 32 → 14.
2. Instrument 8 (`ghz_v04.raw`, FM5's detuned bell arp, FM5 only) 32 → 25: +2.0 dB on the 62 notes
   without `Cxx`, now −0.1 dB.  FM5 is panned right, so this volume includes the pan compensation.
3. Instrument 5 (`ghz_v02.raw`) stays at 32 (FM1 −0.6 dB, FM3 +0.9 dB).  *An earlier revision of
   this audit set it to 24 and instrument 8 to 19 from mono-averaged levels; both were
   over-corrections and were reverted — see "Measurement correction".*
4. Removed instruments 7 and 9 and their `voice_map` / `channel_instrument_map` entries (above).

Not changed: instrument 18 (`psg_tone03.raw`, `fTone_03`) plays no note — it is only the PSG1
header voice and is replaced before the first note.  It is 5 KB; left in so the label stays mapped.

`tests/baselines/ghz_baseline.mod` and `scrap_brain_zone_baseline.mod` were regenerated.

---

## Converter work this audit points at

### Measurement correction: levels must be L/R power, not a mono mix

`vgm_compare.py` used to average L and R before measuring level.  A hard-panned YM2612 channel
then reads ~5 dB below a centred one at the same amplitude (GHZ FM4: L 66.1 dB, R 48.3 dB,
mono-average 61.1 dB, power 63.2 dB), while every libopenmpt MOD channel loses the same ~1 dB —
so the bias lands entirely on hardware-panned channels and made FM4/FM5 look 2 dB hotter than they
are.  Levels now come from the stereo arrays (`load_wav(..., stereo=True)`; `rms()` of a
(frames, 2) array is the power average).  The Title Screen has no `smpsPan` and its numbers did
not move.  Everything in this document is on the corrected measure.

### Volume law and pan law (todo item 4) — the remaining level error in GHZ

Per-instrument level error relative to FM2, measured note by note over L/R power (`Cxx` = volume
command the converter put on those notes):

| Channel (pan) | Inst | `Cxx` | Notes | Error dB |
|---------------|------|-------|-------|----------|
| FM1 (mixed) | 5 | — / `C1A`…`C1F` | 20 / 23 | −0.6 / +5.8…+0.4 |
| FM3 (centre) | 5, 10, 11, 13, 14, 15 | mixed | 114 | −1.5…+0.9 |
| FM4 (left) | 6 | `C14`, `C18` | 20 | +6.7, +4.2 |
| FM4 (left) | 11 / 13 / 14 | `C0B` / `C17` / `C1B` | 58 / 23 / 19 | +1.4 / −0.6 / +1.4 |
| FM5 (right) | 8 | — / `C14`, `C16` | 62 / 20 | −0.1 / +6.7, +3.4 |
| FM5 (right) | 11 / 14 | `C0D` / — | 58 / 19 | +2.8 / +2.9 |
| PSG1, PSG2 | 17, 19, 20 | `C0D` | 192 | −1.0…+0.7 |

PSG is right because its path already uses the chip's 2 dB/step law.  The FM path needs:

- header TL baked into the instrument and `Cxx` from `10^(−ΔTL×0.75/20)` only when
  `smpsAlterVol` moves the channel (fixes the `Cxx` rows);
- −3 dB for channels the song hard-pans, relative to centred ones (fixes FM4/FM5 on 11/14).

**Cost in samples.**  MOD instruments cannot share sample data, so "a variant slot whenever two
channels share an instrument at different levels" duplicates ~61 KB each time.  By TL alone GHZ
needs 2 variants (instrument 5: FM1 `$12` vs FM3 `$14`; instrument 11: FM3 `$1A` vs FM4/FM5
`$1D`) = +122 KB on a 760 KB MOD, 18 → 20 of 31 slots; adding the pan law splits 13 and 14 as well
(FM3 centred vs FM4/FM5 panned) = 4 variants, +248 KB.  The alternative costs nothing: bake the
level of the channel with the most notes and put `Cxx` on the *other* channels' notes — FM3's 16
notes on instrument 5 and its 63 on 11/13/14.  Item 4 should do that by default and offer variants
as an option.

### Grace notes (todo item 3, plus two alternatives)

`EDx` alone does not solve GHZ: the grace and the main note are one tick apart, and with
`ticks_per_row: 2` they share a row half of the time.  Options, in order of fidelity:

- `ticks_per_row: 1` with `target_speed: 2` (BPM 200; row = 25 ms = one tick).  Every tick gets a
  row, cuts resolve to 12.5 ms, pattern data doubles (17 → 33 patterns, +40 KB).
- Converter: turn `smpsNoAttack` + pitch change into a tone portamento (`3xx` at maximum speed) so
  the main note slides without re-triggering the sample — the hardware does not re-attack either.
  Breaks when the two notes fall in different `voice_map` ranges (GHZ: `nC6` → inst 15, `nB5` →
  inst 14), so voice $08's split would have to move.
- Drop the grace note and start the main note on its row (what effectively happens today on
  FM4/FM5, by accident).

### FM frequency convention is an octave off in two places that cancel — fixed 2026-09-18

*(Fixed after this audit: both functions use `2^(21−block)`, every FM `synth_root` was lowered an
octave, output is byte-identical.  The FM pitches quoted above — `synth_root: B2` / `B3`, voice $04
"synthesised at C5" — are in the old convention, one octave above what the config says now.  The
PSG `synth_root: A8` is unaffected.)*

`tools/vgm_analyze._fnum_to_hz` and `ym2612/renderer.freq_to_fnum_block` both use
`2^(20−block)`.  The chip — and the driver's own `MakeFMFrequency(f) = f·2^21/fs` — is
`2^(21−block)`.  The analyzer therefore reports every FM pitch an octave high and the synthesiser
renders an octave below the `synth_root` name; configs tuned from one against the other sound
right (867/867 above).  Anything that mixes this convention with real Hz (`sfx/` uses the
driver's table and is correct) will be an octave out.  Fixing it means changing both functions
and lowering every FM `synth_root` by an octave in one commit.

---

## Tooling added

`tools/vgm_pitch_audit.py` — symbolic pitch audit, no rendering: the VGZ's frequency-register
timeline (key-ons *and* legato pitch changes) against the pitch each MOD note sounds at, computed
from `root` / `synth_root` / finetune.  Exit code 1 on any wrong or missing note.  The per-note
pitch column of `vgm_compare.py` measures audio windows; on GHZ it flagged 245 notes that were
all measurement artefacts of grace notes and legato runs.
