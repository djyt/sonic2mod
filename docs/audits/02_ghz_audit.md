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
| Levels (before) | NOISE +6.9 dB, FM5 +5.5 dB, FM4 +1.9 dB relative to FM2; everything else within 1 dB. |
| Levels (after) | NOISE −0.3 dB, FM5 +3.7 dB, FM4 +1.9 dB, FM1 −0.3, FM3 +0.1, PSG1 −0.7, PSG2 −0.6, DAC −1.0. |
| Why FM4/FM5 stay hot | With `fm_volume_scaling: false` the converter ignores the `smpsHeaderFM` TL (FM4 `$08`, FM5 `$20`, FM3 `$14`) and treats one `smpsAlterVol` step as one *linear* MOD volume unit (≈ 0.3 dB) instead of 0.75 dB.  GHZ uses `smpsAlterVol` for fades and section balance, so the error moves with the music: FM1's fade `C1A`→`C1F` on instrument 5 drifts from +7.6 to +3.1 dB.  Instruments 11/13/14 are shared by FM3/FM4/FM5 at different TLs, so no sample volume fixes all three.  → todo item 4. |
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
2. Instrument 5 (`ghz_v02.raw`) 32 → 24: FM1 measured +2.1 dB and FM3 +3.5 dB on notes without `Cxx`.
3. Instrument 8 (`ghz_v04.raw`, FM5's detuned bell arp) 32 → 19: FM5 only, +4.5 dB.
4. Removed instruments 7 and 9 and their `voice_map` / `channel_instrument_map` entries (above).

Not changed: instrument 18 (`psg_tone03.raw`, `fTone_03`) plays no note — it is only the PSG1
header voice and is replaced before the first note.  It is 5 KB; left in so the label stays mapped.

`tests/baselines/ghz_baseline.mod` and `scrap_brain_zone_baseline.mod` were regenerated.

---

## Converter work this audit points at

### Volume law (todo item 4) — the main accuracy gap in GHZ

Per-instrument level error relative to FM2, measured note by note (`Cxx` = volume command the
converter put on those notes):

| Channel | Inst | `Cxx` | Notes | Error dB |
|---------|------|-------|-------|----------|
| FM1 | 5 | — / `C1A`…`C1F` | 20 / 23 | +2.1 / +7.6…+3.1 |
| FM3 | 10, 11, 13, 14, 15 | mixed | 98 | −1.5…+0.8 |
| FM4 | 6 | `C14`, `C18` | 20 | +8.2, +6.2 |
| FM4 | 11 / 13 / 14 | `C0B` / `C17` / `C1B` | 58 / 23 / 19 | +3.5 / −0.6 / +3.9 |
| FM5 | 8 | — / `C19`, `C1C` | 62 / 20 | +4.5 / +10.2, +7.5 |
| FM5 | 11 / 14 | `C0D` / — | 58 / 19 | +5.0 / +5.3 |
| PSG1, PSG2 | 17, 19, 20 | `C0D` | 192 | −1.0…+0.7 |

(The first three config changes above were derived from the rows without `Cxx`.)  PSG is right
because its path already uses the chip's 2 dB/step law.  The FM path needs the same: header TL
baked into the instrument (or a per-channel variant), `Cxx` from `10^(−ΔTL×0.75/20)` only when
`smpsAlterVol` moves the channel.

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
